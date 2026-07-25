# -*- coding: utf-8 -*-
"""Optional ModsSettingsAPI panel that imports saved equipment sets from
kurzdor's "Auto Equipment Return" mod (a separate, more popular mod many
players already have data for). Everything here degrades quietly if
ModsSettingsAPI isn't installed, or kurzdor's data folder doesn't exist.

kurzdor's .dat files are zlib-compressed pickle(protocol 0) dumps of:
    {'vehicles': {<veh intCD>: [[item intCD, ...], <bool flag>], ...},
     'timestamp': <int>, 'version': 1}
Equipment lists are 3 long (1 preset) or 6 long (2 presets, 3 slots each);
0 means an empty slot. Vehicle/item IDs are native WoT intCDs, the same
space our own config already keys and stores by, so no translation is
needed to reuse them directly.
"""
import os
import glob
import zlib
import pickle

import BigWorld
from PlayerEvents import g_playerEvents

from auto_equip_log import LOG
from auto_equip_i18n import t

_MOD_LINKAGE = 'z4imon.auto_equipment_return.kurzdor_import'
_VAR_FILE = 'kurzdorFile'

_g_files = []   # dropdown option index -> full .dat path, built in register()


def _source_dir():
    appdata = os.getenv('APPDATA', '')
    return os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'mods', 'kurzdor', 'autoequipmentreturn')


def _list_dat_files():
    directory = _source_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(glob.glob(os.path.join(directory, '*.dat')))


def _load_dat(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    return pickle.loads(zlib.decompress(raw))


def _account_id():
    try:
        from gui.shared.utils import getPlayerDatabaseID
        return getPlayerDatabaseID()
    except Exception:
        LOG.exc('auto_equip_import._account_id failed')
        return 0


def _sanitize(raw):
    out = []
    for cd in raw:
        try:
            out.append(int(cd))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _import_from_file(path):
    """Imports every vehicle entry from a kurzdor .dat into our own config.
    Vehicles that already have saved sets in our config are left untouched
    (existing data always wins over an import)."""
    import mod_auto_equip
    obj = _load_dat(path)
    vehicles = obj.get('vehicles', {}) if isinstance(obj, dict) else {}
    imported = 0
    skipped = 0
    for veh_id, entry in vehicles.iteritems():
        try:
            veh_cd = int(veh_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        equip = entry[0]
        if not isinstance(equip, (list, tuple)) or not equip:
            continue
        if mod_auto_equip.get_sets(veh_cd) is not None:
            skipped += 1
            continue
        equip = _sanitize(equip)
        set1 = equip[0:3]
        set2 = equip[3:6] if len(equip) >= 6 else None
        mod_auto_equip.store_sets(veh_cd, set1=set1, set2=set2)
        imported += 1
    return imported, skipped


def _push_result(imported, skipped, filename):
    from auto_equip_core import _push_msg
    if imported == 0 and skipped == 0:
        _push_msg(t('importEmptyMsg', file=filename), warning=True)
    else:
        _push_msg(t('importDoneMsg', imported=imported, skipped=skipped, file=filename))


def onButtonClicked(linkage, varName, value):
    if linkage != _MOD_LINKAGE or varName != _VAR_FILE:
        return
    try:
        index = int(value)
    except (TypeError, ValueError):
        return
    if index < 0 or index >= len(_g_files):
        return
    path = _g_files[index]
    try:
        imported, skipped = _import_from_file(path)
        _push_result(imported, skipped, os.path.basename(path))
        LOG.info('kurzdor import from %s: %d imported, %d skipped' % (path, imported, skipped))
    except Exception:
        LOG.exc('auto_equip_import.onButtonClicked failed')
        try:
            from auto_equip_core import _push_msg
            _push_msg(t('importFailedMsg'), error=True)
        except Exception:
            pass


def onModSettingsChanged(linkage, newSettings):
    pass


def register():
    """Add the ModsSettingsAPI panel (no-op if the API or kurzdor's data
    folder aren't present)."""
    global _g_files
    try:
        from gui.modsSettingsApi import g_modsSettingsApi  # noqa: F401
    except Exception:
        LOG.info('ModsSettingsAPI not installed - kurzdor import disabled')
        return

    _g_files = _list_dat_files()
    if not _g_files:
        LOG.info('kurzdor auto-equipment-return folder not found or empty - import disabled')
        return

    account_id = _account_id()
    if account_id:
        _finish_register(account_id)
        return

    # mod init() runs before login. Account.databaseID is explicitly reset
    # to None in Account.onBecomePlayer() and only filled in later, right
    # before Account.showGUI() fires g_playerEvents.onAccountShowGUI - wait
    # for that instead of guessing a timeout.
    LOG.info('kurzdor import: account id not ready yet, waiting for onAccountShowGUI')
    g_playerEvents.onAccountShowGUI += _on_account_show_gui


def _on_account_show_gui(ctx=None):
    g_playerEvents.onAccountShowGUI -= _on_account_show_gui
    _finish_register(_account_id())


def _finish_register(account_id):
    from gui.modsSettingsApi import g_modsSettingsApi, templates

    options = [os.path.splitext(os.path.basename(p))[0] for p in _g_files]

    template = {
        'modDisplayName': t('importModDisplayName'),
        'column1': [
            templates.createLabel(t('importAccountLabel', accountId=account_id)),
            templates.createDropdown(
                t('importDropdownLabel'), _VAR_FILE, options, 0,
                button=templates.createButton(text=t('importButtonText'), width=70, height=23)
            ),
        ],
    }

    try:
        savedSettings = g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template)
        if savedSettings:
            g_modsSettingsApi.registerCallback(_MOD_LINKAGE, onModSettingsChanged, onButtonClicked)
        else:
            g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template, onModSettingsChanged, onButtonClicked)
        player = BigWorld.player()
        LOG.info('registered kurzdor import panel (%d file(s), accountId=%s, player=%s, rawDatabaseID=%s)' % (
            len(_g_files), account_id, player is not None, getattr(player, 'databaseID', None)))
    except Exception:
        LOG.exc('auto_equip_import.register failed')
