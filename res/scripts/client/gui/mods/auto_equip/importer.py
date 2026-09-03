# -*- coding: utf-8 -*-
"""The mod's optional ModsSettingsAPI panel, and the save-file importing that
is most of what it offers.

The panel holds two things:

* the CLEANUP action (cleanup.py) - one dropdown picking how wide to go and a
  button that runs it. Always there, because it needs nothing but saved sets;
* the IMPORT section, two ways to seed this account's saved sets instead of
  saving everything by hand again:

  1. from kurzdor's "Auto Equipment Return" mod - a separate, more popular mod
     many players already have data for. This also runs silently, without any
     panel, the first time our mod sees a brand-new account (see
     auto_import_for_account, called from config.py).
  2. from one of OUR OWN mod's other per-account save files, for players with
     several WoT accounts on this PC.

  It only appears when at least one of those data sources has something to
  offer, which for most players is never.

Everything here degrades quietly if ModsSettingsAPI isn't installed.

kurzdor's .dat files are zlib-compressed pickle (protocol 0) dumps of:

    {'vehicles': {<veh invID>: [[item intCD, ...], <bool flag>], ...},
     'timestamp': <int>, 'version': 1}

Equipment lists hold 3 entries (one preset) or 6 (two presets of 3 slots);
0 means an empty slot. The equipment ids are native WoT intCDs, and the
vehicle key is the vehicle's INVENTORY id, not its type compactDescr.
(Confirmed: his vehicle keys don't decode as valid compactDescrs, they sit in
the same small numeric range as WoT invIDs, and his own account's plaintext
save uses the same key for the same vehicle we save under.)

Our config keys by invID too, so kurzdor's keys can be reused directly - but
ONLY within the same account, since an invID is an inventory-instance id
assigned per account. His key only lines up with ours when both files belong
to the same WoT account, which is matched by filename == account id. Importing
one of his files from a DIFFERENT account would silently put equipment on the
wrong tanks, so the panel marks those files and refuses the click, pointing at
the detour that does work (import them on their own account, then import THAT
account's file of ours here).

Our own save files have no such limit, because we also record the vehicleCD
(the account-independent type compactDescr). Importing one into a different
account remaps every entry through that vehicleCD to whatever invID the vehicle
type has here. Entries saved before vehicleCD was recorded, or for vehicle
types this account doesn't own, are reported as unmatched.
"""

import glob
import json
import os
import pickle
import zlib

from . import cleanup, config, inventory, messages
from .i18n import t
from .log import LOG

# Named after what the panel started out as. Kept unchanged on purpose: the
# linkage is the key ModsSettingsAPI stores this mod's panel state under, so
# renaming it would orphan every player's saved panel state.
_MOD_LINKAGE = 'z4imon.auto_equipment_return.kurzdor_import'
_VAR_KURZDOR_FILE = 'kurzdorFile'
_VAR_OWN_FILE = 'ownAccountFile'
_VAR_CLEANUP_SCOPE = 'cleanupScope'

_SETS_PER_KURZDOR_ENTRY = 3

# The one-line note under the kurzdor dropdown. See _other_account_note for
# why it stays a single short label and may not exceed _LABEL_MAX_CHARS.
_NOTE_KEY = 'importOtherAccountWhy'
_LABEL_MAX_CHARS = 130

# Dropdown option index -> full path, and the account we're logged in as.
# All three are filled in by register().
_kurzdor_files = []
_own_files = []
_account_id = None


# ---------------------------------------------------------------------------
# Finding save files
# ---------------------------------------------------------------------------

def _kurzdor_dir():
    return os.path.join(config.mods_dir(), 'kurzdor', 'autoequipmentreturn')


def _file_label(path):
    """Both kurzdor's .dat files and our own .json files are named after the
    account they belong to, so the bare filename IS the account id."""
    return os.path.splitext(os.path.basename(path))[0]


def _belongs_to_this_account(path):
    return _account_id is not None and _file_label(path) == str(_account_id)


def _list_kurzdor_files():
    """Own-account file first, so the dropdown's default selection is the one
    that can actually be imported."""
    directory = _kurzdor_dir()
    if not os.path.isdir(directory):
        return []
    paths = glob.glob(os.path.join(directory, '*.dat'))
    return sorted(paths, key=lambda path: (not _belongs_to_this_account(path), path))


def _list_own_account_files():
    directory = config.account_files_dir()
    if not os.path.isdir(directory):
        return []
    return [path for path in sorted(glob.glob(os.path.join(directory, '*.json')))
            if not _belongs_to_this_account(path)]


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------

def _as_device_cds(raw):
    cds = []
    for cd in raw:
        try:
            cds.append(int(cd))
        except (TypeError, ValueError):
            cds.append(0)
    return cds


def _read_kurzdor_file(path):
    with open(path, 'rb') as handle:
        return pickle.loads(zlib.decompress(handle.read()))


def _kurzdor_entry_sets(entry):
    """The two set lists of one kurzdor vehicle entry, or (None, None) when the
    entry carries no usable equipment."""
    if not isinstance(entry, (list, tuple)) or not entry:
        return None, None
    equipment = entry[0]
    if not isinstance(equipment, (list, tuple)) or not equipment:
        return None, None
    cds = _as_device_cds(equipment)
    set1 = cds[:_SETS_PER_KURZDOR_ENTRY]
    set2 = cds[_SETS_PER_KURZDOR_ENTRY:2 * _SETS_PER_KURZDOR_ENTRY] or None
    return set1, set2


def _import_kurzdor_file(path):
    """Imports every vehicle entry from a kurzdor .dat. Vehicles that already
    have saved sets are left untouched - existing data always beats an import.

    Because his format carries no vehicle type id (see the module docstring),
    every import finishes with a bulk pass that fills vehicleCD in from the
    account's actual owned vehicles. Returns (imported, skipped)."""
    data = _read_kurzdor_file(path)
    vehicles = data.get('vehicles', {}) if isinstance(data, dict) else {}
    imported = 0
    skipped = 0
    for raw_veh_id, entry in vehicles.iteritems():
        try:
            veh_inv_id = int(raw_veh_id)
        except (TypeError, ValueError):
            continue
        set1, set2 = _kurzdor_entry_sets(entry)
        if set1 is None:
            continue
        if config.has_saved_sets(veh_inv_id):
            skipped += 1
            continue
        config.store_sets(veh_inv_id, set1=set1, set2=set2)
        imported += 1
    inventory.fill_in_missing_vehicle_cds()
    return imported, skipped


def _import_own_account_file(path):
    """Imports every entry from ANOTHER of our own account files, remapping
    each one onto this account's invID for that vehicle TYPE - the source
    account's invID means nothing here. Entries without a recorded vehicleCD,
    or for vehicles this account doesn't own, count as unmatched.
    Returns (imported, skipped, unmatched)."""
    with open(path, 'r') as handle:
        data = json.load(handle)
    sets = data.get('sets', {}) if isinstance(data, dict) else {}
    imported = 0
    skipped = 0
    unmatched = 0
    for entry in sets.itervalues():
        if not isinstance(entry, dict):
            continue
        set1 = entry.get('set1')
        set2 = entry.get('set2')
        if set1 is None and set2 is None:
            continue
        veh_cd = entry.get('vehicleCD')
        veh_inv_id = inventory.inv_id_for_vehicle_type(int(veh_cd)) if veh_cd else None
        if veh_inv_id is None:
            unmatched += 1
            continue
        if config.has_saved_sets(veh_inv_id):
            skipped += 1
            continue
        config.store_sets(veh_inv_id, set1=set1, set2=set2, veh_cd=veh_cd)
        imported += 1
    return imported, skipped, unmatched


def auto_import_for_account(account_id):
    """Called once by config.py right after a fresh, empty config was
    created for this account: silently pulls in kurzdor's save for the same
    account id, if he has one, so returning players don't start from scratch.
    No panel and no messages - this runs before the popover even exists."""
    path = os.path.join(_kurzdor_dir(), '%s.dat' % account_id)
    if not os.path.isfile(path):
        return
    try:
        imported, skipped = _import_kurzdor_file(path)
        LOG.info('kurzdor first-run auto-import (%s): %d imported, %d skipped'
                 % (path, imported, skipped))
    except Exception:
        LOG.exc('auto_import_for_account failed')


# ---------------------------------------------------------------------------
# Panel callbacks
# ---------------------------------------------------------------------------

def _refuse_other_account(path):
    """kurzdor's vehicle keys are inventory ids, which only mean anything
    inside the account they were written for (see the module docstring), so
    importing his file from another account is refused rather than silently
    equipping the wrong tanks. Says why, and how to get the data across
    anyway."""
    LOG.info('refused kurzdor import from %s: belongs to account %s, we are %s'
             % (path, _file_label(path), _account_id))
    messages.push_lines([t('importOtherAccountMsg', file=os.path.basename(path),
                           fileAccount=_file_label(path), accountId=_account_id),
                         t('importOtherAccountStep1'),
                         t('importOtherAccountStep2')], warning=True)


def _handle_kurzdor_import(index):
    if not 0 <= index < len(_kurzdor_files):
        return
    path = _kurzdor_files[index]
    if not _belongs_to_this_account(path):
        _refuse_other_account(path)
        return
    try:
        imported, skipped = _import_kurzdor_file(path)
        filename = os.path.basename(path)
        if imported == 0 and skipped == 0:
            messages.push_warning(t('importEmptyMsg', file=filename))
        else:
            messages.push_info(t('importDoneMsg', imported=imported, skipped=skipped,
                                 file=filename))
        LOG.info('kurzdor import from %s: %d imported, %d skipped'
                 % (path, imported, skipped))
    except Exception:
        LOG.exc('_handle_kurzdor_import failed')
        messages.push_error(t('importFailedMsg'))


def _handle_own_import(index):
    if not 0 <= index < len(_own_files):
        return
    path = _own_files[index]
    try:
        imported, skipped, unmatched = _import_own_account_file(path)
        filename = os.path.basename(path)
        if imported == 0 and skipped == 0 and unmatched == 0:
            messages.push_warning(t('importEmptyMsg', file=filename))
        else:
            messages.push_info(t('importOwnDoneMsg', imported=imported, skipped=skipped,
                                 unmatched=unmatched, file=filename))
        LOG.info('own-account import from %s: %d imported, %d skipped, %d unmatched'
                 % (path, imported, skipped, unmatched))
    except Exception:
        LOG.exc('_handle_own_import failed')
        messages.push_error(t('importFailedMsg'))


def onButtonClicked(linkage, varName, value):
    if linkage != _MOD_LINKAGE:
        return
    try:
        index = int(value)
    except (TypeError, ValueError):
        return
    if varName == _VAR_KURZDOR_FILE:
        _handle_kurzdor_import(index)
    elif varName == _VAR_OWN_FILE:
        _handle_own_import(index)
    elif varName == _VAR_CLEANUP_SCOPE:
        _handle_cleanup(index)


def _handle_cleanup(scope):
    """The dropdown index IS the scope - see cleanup.SCOPE_*. The run reports
    itself, so there is nothing to say here."""
    try:
        cleanup.demount_misplaced(scope)
    except Exception:
        LOG.exc('_handle_cleanup failed')


def onModSettingsChanged(linkage, newSettings):
    pass


# ---------------------------------------------------------------------------
# Panel registration
# ---------------------------------------------------------------------------

def _has_other_account_files():
    return any(not _belongs_to_this_account(path) for path in _kurzdor_files)


def _kurzdor_options():
    """One dropdown entry per kurzdor file, each labelled with the account it
    belongs to. ModsSettingsAPI can only grey out a whole mod panel, never a
    single button, so the entries that _handle_kurzdor_import will refuse say
    so in their own label and carry the explanation as a per-entry tooltip."""
    options = []
    for path in _kurzdor_files:
        account = _file_label(path)
        if _belongs_to_this_account(path):
            options.append(t('importOptionOwnAccount', account=account))
        else:
            options.append((t('importOptionOtherAccount', account=account),
                            t('importOtherAccountTooltip')))
    return options


def _other_account_note(account_id, templates):
    """One short line stating the rule, with the full explanation and the
    detour behind its info icon.

    Panel labels are a fixed 800px, never wrap, silently clip what doesn't fit
    and render "\\n" as a space, so prose simply doesn't belong in them - no
    other mod in a shared modsettings.dat ships a label over 50 characters.
    Tooltips are the opposite renderer and do break on "\\n", as do 166 of the
    852 tooltip bodies in the client's own tooltips.mo, none of which use
    <br>."""
    if not _has_other_account_files():
        return []
    return [templates.createLabel(t(_NOTE_KEY, accountId=account_id),
                                  tooltip=t('importOtherAccountTooltip'))]


def _import_button(templates):
    return templates.createButton(text=t('importButtonText'), width=70, height=23)


def _cleanup_row(templates):
    """The cleanup action. ModsSettingsAPI has no standalone button component -
    a button only ever rides along with a control - so the scope dropdown is
    both the setting and the button's host, exactly like the import rows."""
    return templates.createDropdown(
        t('cleanupLabel'), _VAR_CLEANUP_SCOPE,
        [t('cleanupScopeAll'), t('cleanupScopePrimary')], 0,
        tooltip=t('cleanupTooltip'),
        button=templates.createButton(text=t('cleanupButtonText'),
                                      width=100, height=23))


def _import_rows(templates):
    rows = []
    if _kurzdor_files:
        rows.append(templates.createDropdown(
            t('importDropdownLabel'), _VAR_KURZDOR_FILE, _kurzdor_options(), 0,
            tooltip=t('importOtherAccountTooltip') if _has_other_account_files() else None,
            button=_import_button(templates)))
        rows.extend(_other_account_note(_account_id, templates))
    if _own_files:
        rows.append(templates.createDropdown(
            t('importOwnDropdownLabel'), _VAR_OWN_FILE,
            [_file_label(path) for path in _own_files], 0,
            button=_import_button(templates)))
    return rows


def _build_column(account_id, templates):
    """Cleanup first: it is the everyday action, while importing is a one-off
    most players never do - and for most of them the import section is not
    there at all."""
    column = [_cleanup_row(templates)]
    rows = _import_rows(templates)
    if rows:
        column.append(templates.createEmpty())
        column.append(templates.createLabel(t('importAccountLabel', accountId=account_id)))
        column.extend(rows)
    return column


def _get_settings_api():
    """(g_modsSettingsApi, templates), preferring Aslain's Mod Menu over
    whatever else may answer to the old gui.modsSettingsApi name - (None,
    None) if neither is installed.

    Aslain's own integration guide is explicit that the two names are NOT
    interchangeable: gui.aslainMenu is only ever Aslain's menu, while
    gui.modsSettingsApi is whichever package happened to claim that name -
    Aslain's own menu when nothing else did, but izeberg's original or some
    other reimplementation when another mod (often bundled in the same pack)
    got there first. Importing gui.modsSettingsApi directly, as this used to,
    skips that check entirely: with Aslain's Mod Menu installed alongside
    something else that also claims the old name, this panel could silently
    end up wired to that OTHER implementation's tooltip renderer instead of
    Aslain's - which is the reported symptom (this panel's {HEADER}/{BODY}
    tooltips not showing correctly under Aslain's Mod Menu), even though
    Aslain's own renderer documents support for exactly that markup."""
    try:
        from gui.aslainMenu import g_modsSettingsApi, templates
        return g_modsSettingsApi, templates
    except ImportError:
        pass
    try:
        from gui.modsSettingsApi import g_modsSettingsApi, templates
        return g_modsSettingsApi, templates
    except ImportError:
        return None, None


def register(account_id):
    """Adds the ModsSettingsAPI panel. No-op when the API isn't installed.
    Called by mod_auto_equip once the account id is known - never before.

    The panel used to bow out when there was nothing to import; it no longer
    can, because the cleanup action stands on its own."""
    global _kurzdor_files, _own_files, _account_id
    g_modsSettingsApi, templates = _get_settings_api()
    if g_modsSettingsApi is None:
        LOG.info('ModsSettingsAPI not installed - settings panel disabled')
        return

    _account_id = account_id
    _kurzdor_files = _list_kurzdor_files()
    _own_files = _list_own_account_files()

    template = {
        'modDisplayName': t('panelDisplayName'),
        'enabled': True,
        'column1': _build_column(account_id, templates),
    }
    try:
        if g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template):
            g_modsSettingsApi.registerCallback(_MOD_LINKAGE, onModSettingsChanged, onButtonClicked)
        else:
            g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template,
                                             onModSettingsChanged, onButtonClicked)
        LOG.info('registered settings panel (%d kurzdor file(s), %d own account file(s), accountId=%s)'
                 % (len(_kurzdor_files), len(_own_files), account_id))
    except Exception:
        LOG.exc('register failed')
