# -*- coding: utf-8 -*-
"""Optional ModsSettingsAPI panel offering two ways to seed the current
account's saved equipment sets instead of saving them all by hand again:

1. Import from kurzdor's "Auto Equipment Return" mod (a separate, more
   popular mod many players already have data for). Also runs silently,
   without any panel, the first time our own mod sees a brand-new account
   (see auto_import_for_account, called from mod_auto_equip.load_config).
2. Import from one of OUR OWN mod's other per-account save files, for
   players with more than one WoT account on this PC.

Everything here degrades quietly if ModsSettingsAPI isn't installed, or
neither data source has anything to offer.

kurzdor's .dat files are zlib-compressed pickle(protocol 0) dumps of:
    {'vehicles': {<veh invID>: [[item intCD, ...], <bool flag>], ...},
     'timestamp': <int>, 'version': 1}
Equipment lists are 3 long (1 preset) or 6 long (2 presets, 3 slots each);
0 means an empty slot. The equipment IDs are native WoT intCDs; the vehicle
key is the vehicle's inventory ID (invID), NOT its type compactDescr
(confirmed: kurzdor's vehicle keys don't decode as valid compactDescrs, and
are in the same small numeric range as WoT invIDs, and his own account's
plaintext save file uses the same key for the same vehicle we save under).
Our own config now keys by invID too (see mod_auto_equip.py), so no
translation is needed to reuse kurzdor's vehicle keys directly — but ONLY
within the same account: invID is an inventory-instance id assigned
per-account, so kurzdor's key only lines up with OUR key when both files
belong to the same WoT account (matched by filename == account id).

Our own per-account save files use the same invID keying, which means they
too can only be imported directly into THAT SAME account. Importing into a
DIFFERENT account instead remaps every entry via its stored vehicleCD (type
compactDescr, account-independent) to whatever invID that vehicle type has
in the CURRENT account's own inventory — entries saved before vehicleCD was
recorded, or for vehicle types the current account doesn't own, are skipped.
"""
import os
import glob
import zlib
import pickle

from auto_equip_log import LOG
from auto_equip_i18n import t

_MOD_LINKAGE = 'z4imon.auto_equipment_return.kurzdor_import'
_VAR_KURZDOR_FILE = 'kurzdorFile'
_VAR_OWN_FILE = 'ownAccountFile'

_g_kurzdor_files = []   # dropdown option index -> full .dat path
_g_own_files = []       # dropdown option index -> full .json path (other accounts)


def _kurzdor_dir():
    appdata = os.getenv('APPDATA', '')
    return os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'mods', 'kurzdor', 'autoequipmentreturn')


def _list_kurzdor_files():
    directory = _kurzdor_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(glob.glob(os.path.join(directory, '*.dat')))


def _list_own_account_files(exclude_account_id):
    import mod_auto_equip
    directory = mod_auto_equip.account_files_dir()
    if not os.path.isdir(directory):
        return []
    exclude_name = '%s.json' % exclude_account_id
    files = sorted(glob.glob(os.path.join(directory, '*.json')))
    return [p for p in files if os.path.basename(p) != exclude_name]


def _load_kurzdor_dat(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    return pickle.loads(zlib.decompress(raw))


def _sanitize(raw):
    out = []
    for cd in raw:
        try:
            out.append(int(cd))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _import_from_kurzdor_file(path):
    """Imports every vehicle entry from a kurzdor .dat into our own config.
    Vehicles that already have saved sets in our config are left untouched
    (existing data always wins over an import).

    kurzdor's save format has no vehicle-type id at all (see the module
    docstring) - only his own account-scoped vehicle key, which we trust as
    this account's real invID. That's fine for THIS import, but it leaves
    every entry (both the ones just imported and any older ones already
    sitting in the config) without a vehicleCD - and vehicleCD is exactly
    what a LATER cross-account import needs to remap a set correctly onto a
    DIFFERENT WoT account. So every kurzdor import finishes with a bulk
    backfill pass over the account's actual owned vehicles (reading invID
    AND intCD straight off each live vehicle object) to fill that in."""
    import mod_auto_equip
    import auto_equip_core
    obj = _load_kurzdor_dat(path)
    vehicles = obj.get('vehicles', {}) if isinstance(obj, dict) else {}
    imported = 0
    skipped = 0
    for veh_id, entry in vehicles.iteritems():
        try:
            veh_inv_id = int(veh_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        equip = entry[0]
        if not isinstance(equip, (list, tuple)) or not equip:
            continue
        if mod_auto_equip.get_sets(veh_inv_id) is not None:
            skipped += 1
            continue
        equip = _sanitize(equip)
        set1 = equip[0:3]
        set2 = equip[3:6] if len(equip) > 3 else None
        mod_auto_equip.store_sets(veh_inv_id, set1=set1, set2=set2)
        imported += 1
    auto_equip_core.backfill_vehicle_cds()
    return imported, skipped


def _import_from_own_account_file(path):
    """Imports every vehicle entry from ANOTHER of our own account's save
    files, remapping each entry's invID key to the current account's own
    invID for that vehicle TYPE (via the stored vehicleCD) — the source
    account's invID itself means nothing here. Entries without a recorded
    vehicleCD (saved before that field existed) or for a vehicle type the
    current account doesn't own are skipped as unmatched."""
    import json
    import mod_auto_equip
    import auto_equip_core
    with open(path, 'r') as fh:
        data = json.load(fh)
    sets = data.get('sets', {}) if isinstance(data, dict) else {}
    imported = 0
    skipped = 0
    unmatched = 0
    for _, entry in sets.iteritems():
        if not isinstance(entry, dict):
            continue
        set1 = entry.get('set1')
        set2 = entry.get('set2')
        if set1 is None and set2 is None:
            continue
        veh_cd = entry.get('vehicleCD')
        if not veh_cd:
            unmatched += 1
            continue
        veh_inv_id = auto_equip_core.resolve_inv_id_by_cd(int(veh_cd))
        if veh_inv_id is None:
            unmatched += 1
            continue
        if mod_auto_equip.get_sets(veh_inv_id) is not None:
            skipped += 1
            continue
        mod_auto_equip.store_sets(veh_inv_id, set1=set1, set2=set2, veh_cd=veh_cd)
        imported += 1
    return imported, skipped, unmatched


def auto_import_for_account(account_id):
    """Called once by mod_auto_equip right after a fresh (empty) config is
    created for this account — silently pulls in kurzdor's save for the same
    account id, if he has one, so returning players don't start from scratch.
    No panel/messages involved; this runs before the popover even exists."""
    path = os.path.join(_kurzdor_dir(), '%s.dat' % account_id)
    if not os.path.isfile(path):
        return
    try:
        imported, skipped = _import_from_kurzdor_file(path)
        LOG.info('kurzdor first-run auto-import (%s): %d imported, %d skipped' % (path, imported, skipped))
    except Exception:
        LOG.exc('auto_equip_import.auto_import_for_account failed')


def _push_kurzdor_result(imported, skipped, filename):
    from auto_equip_core import _push_msg
    if imported == 0 and skipped == 0:
        _push_msg(t('importEmptyMsg', file=filename), warning=True)
    else:
        _push_msg(t('importDoneMsg', imported=imported, skipped=skipped, file=filename))


def _push_own_result(imported, skipped, unmatched, filename):
    from auto_equip_core import _push_msg
    if imported == 0 and skipped == 0 and unmatched == 0:
        _push_msg(t('importEmptyMsg', file=filename), warning=True)
    else:
        _push_msg(t('importOwnDoneMsg', imported=imported, skipped=skipped, unmatched=unmatched, file=filename))


def _push_failed():
    try:
        from auto_equip_core import _push_msg
        _push_msg(t('importFailedMsg'), error=True)
    except Exception:
        pass


def _handle_kurzdor_import(index):
    if index < 0 or index >= len(_g_kurzdor_files):
        return
    path = _g_kurzdor_files[index]
    try:
        imported, skipped = _import_from_kurzdor_file(path)
        _push_kurzdor_result(imported, skipped, os.path.basename(path))
        LOG.info('kurzdor import from %s: %d imported, %d skipped' % (path, imported, skipped))
    except Exception:
        LOG.exc('auto_equip_import._handle_kurzdor_import failed')
        _push_failed()


def _handle_own_import(index):
    if index < 0 or index >= len(_g_own_files):
        return
    path = _g_own_files[index]
    try:
        imported, skipped, unmatched = _import_from_own_account_file(path)
        _push_own_result(imported, skipped, unmatched, os.path.basename(path))
        LOG.info('own-account import from %s: %d imported, %d skipped, %d unmatched'
                 % (path, imported, skipped, unmatched))
    except Exception:
        LOG.exc('auto_equip_import._handle_own_import failed')
        _push_failed()


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


def register(account_id):
    """Add the ModsSettingsAPI panel (no-op if the API isn't present, or
    neither data source has anything to offer). Called by mod_auto_equip once
    the account id is known — never before."""
    global _g_kurzdor_files, _g_own_files
    try:
        from gui.modsSettingsApi import g_modsSettingsApi  # noqa: F401
    except Exception:
        LOG.info('ModsSettingsAPI not installed - import panel disabled')
        return

    _g_kurzdor_files = _list_kurzdor_files()
    _g_own_files = _list_own_account_files(account_id)
    if not _g_kurzdor_files and not _g_own_files:
        LOG.info('no kurzdor data and no other account files - import panel disabled')
        return

    from gui.modsSettingsApi import templates

    column1 = [templates.createLabel(t('importAccountLabel', accountId=account_id))]

    if _g_kurzdor_files:
        options = [os.path.splitext(os.path.basename(p))[0] for p in _g_kurzdor_files]
        column1.append(templates.createDropdown(
            t('importDropdownLabel'), _VAR_KURZDOR_FILE, options, 0,
            button=templates.createButton(text=t('importButtonText'), width=70, height=23)
        ))

    if _g_own_files:
        own_options = [os.path.splitext(os.path.basename(p))[0] for p in _g_own_files]
        column1.append(templates.createDropdown(
            t('importOwnDropdownLabel'), _VAR_OWN_FILE, own_options, 0,
            button=templates.createButton(text=t('importButtonText'), width=70, height=23)
        ))

    template = {
        'modDisplayName': t('importModDisplayName'),
        'enabled': True,
        'column1': column1,
    }

    try:
        savedSettings = g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template)
        if savedSettings:
            g_modsSettingsApi.registerCallback(_MOD_LINKAGE, onModSettingsChanged, onButtonClicked)
        else:
            g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template, onModSettingsChanged, onButtonClicked)
        LOG.info('registered import panel (%d kurzdor file(s), %d own account file(s), accountId=%s)'
                 % (len(_g_kurzdor_files), len(_g_own_files), account_id))
    except Exception:
        LOG.exc('auto_equip_import.register failed')
