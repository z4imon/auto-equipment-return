# -*- coding: utf-8 -*-
"""Optional ModsSettingsAPI panel offering two ways to seed this account's
saved sets instead of saving everything by hand again:

1. Import from kurzdor's "Auto Equipment Return" mod - a separate, more
   popular mod many players already have data for. This also runs silently,
   without any panel, the first time our mod sees a brand-new account (see
   auto_import_for_account, called from auto_equip_config).
2. Import from one of OUR OWN mod's other per-account save files, for players
   with several WoT accounts on this PC.

Everything here degrades quietly if ModsSettingsAPI isn't installed, or if
neither data source has anything to offer.

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
to the same WoT account, which is matched by filename == account id.

The same applies to our own save files, which is why importing into a
DIFFERENT account remaps every entry through its stored vehicleCD (the
account-independent type compactDescr) to whatever invID that vehicle type has
in the current account. Entries saved before vehicleCD was recorded, or for
vehicle types this account doesn't own, are reported as unmatched.
"""

import glob
import json
import os
import pickle
import zlib

import auto_equip_config as config
import auto_equip_inventory as inventory
import auto_equip_messages as messages
from auto_equip_i18n import t
from auto_equip_log import LOG

_MOD_LINKAGE = 'z4imon.auto_equipment_return.kurzdor_import'
_VAR_KURZDOR_FILE = 'kurzdorFile'
_VAR_OWN_FILE = 'ownAccountFile'

_SETS_PER_KURZDOR_ENTRY = 3

# Dropdown option index -> full path, filled in by register().
_kurzdor_files = []
_own_files = []


# ---------------------------------------------------------------------------
# Finding save files
# ---------------------------------------------------------------------------

def _kurzdor_dir():
    appdata = os.getenv('APPDATA', '')
    return os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'mods',
                        'kurzdor', 'autoequipmentreturn')


def _list_kurzdor_files():
    directory = _kurzdor_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(glob.glob(os.path.join(directory, '*.dat')))


def _list_own_account_files(exclude_account_id):
    directory = config.account_files_dir()
    if not os.path.isdir(directory):
        return []
    exclude_name = '%s.json' % exclude_account_id
    return [path for path in sorted(glob.glob(os.path.join(directory, '*.json')))
            if os.path.basename(path) != exclude_name]


def _file_label(path):
    return os.path.splitext(os.path.basename(path))[0]


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
        if config.saved_sets(veh_inv_id) is not None:
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
        if config.saved_sets(veh_inv_id) is not None:
            skipped += 1
            continue
        config.store_sets(veh_inv_id, set1=set1, set2=set2, veh_cd=veh_cd)
        imported += 1
    return imported, skipped, unmatched


def auto_import_for_account(account_id):
    """Called once by auto_equip_config right after a fresh, empty config was
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

def _handle_kurzdor_import(index):
    if not 0 <= index < len(_kurzdor_files):
        return
    path = _kurzdor_files[index]
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


def onModSettingsChanged(linkage, newSettings):
    pass


# ---------------------------------------------------------------------------
# Panel registration
# ---------------------------------------------------------------------------

def _build_column(account_id, templates):
    column = [templates.createLabel(t('importAccountLabel', accountId=account_id))]
    dropdowns = ((_kurzdor_files, _VAR_KURZDOR_FILE, 'importDropdownLabel'),
                 (_own_files, _VAR_OWN_FILE, 'importOwnDropdownLabel'))
    for files, var_name, label_key in dropdowns:
        if not files:
            continue
        column.append(templates.createDropdown(
            t(label_key), var_name, [_file_label(path) for path in files], 0,
            button=templates.createButton(text=t('importButtonText'), width=70, height=23)))
    return column


def register(account_id):
    """Adds the ModsSettingsAPI panel. No-op when the API isn't installed, or
    when neither data source has anything to offer. Called by mod_auto_equip
    once the account id is known - never before."""
    global _kurzdor_files, _own_files
    try:
        from gui.modsSettingsApi import g_modsSettingsApi, templates
    except Exception:
        LOG.info('ModsSettingsAPI not installed - import panel disabled')
        return

    _kurzdor_files = _list_kurzdor_files()
    _own_files = _list_own_account_files(account_id)
    if not _kurzdor_files and not _own_files:
        LOG.info('no kurzdor data and no other account files - import panel disabled')
        return

    template = {
        'modDisplayName': t('importModDisplayName'),
        'enabled': True,
        'column1': _build_column(account_id, templates),
    }
    try:
        if g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template):
            g_modsSettingsApi.registerCallback(_MOD_LINKAGE, onModSettingsChanged, onButtonClicked)
        else:
            g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template,
                                             onModSettingsChanged, onButtonClicked)
        LOG.info('registered import panel (%d kurzdor file(s), %d own account file(s), accountId=%s)'
                 % (len(_kurzdor_files), len(_own_files), account_id))
    except Exception:
        LOG.exc('register failed')
