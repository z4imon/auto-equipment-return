import json
import os

from auto_equip_log import LOG

# --------------------------------------------------------------------------
# Persisted configuration (hand-editable plain JSON, per user preference)
#
# One file per account: .../mods/z4imon/autoequipmentreturn/<accountId>.json
# (same folder shape kurzdor's mod uses), so switching between accounts on the
# same PC never mixes up saved sets.
#
# sets: { "<vehicle invID>": { "set1": [intCD, intCD, intCD] or null,
#                              "set2": [intCD, intCD, intCD] or null,
#                              "vehicleCD": intCD or null } }
# Keyed by the vehicle's inventory ID (not its type compactDescr) so imported
# data from kurzdor's Auto Equipment Return mod - which uses the same
# invID-based scheme - lines up without translation. vehicleCD (the vehicle's
# type compactDescr) is recorded alongside purely so a set can be remapped to
# a DIFFERENT account's own invID for that vehicle type later (invID is only
# meaningful within the account that assigned it) — see auto_equip_import.py.
# A 0 inside a set list means "slot empty on purpose".
# --------------------------------------------------------------------------

CONFIG = {
    'autoEquipEnabled': True,   # global switch for the automatic install on vehicle selection
    'downgradeEnabled': False,  # replace unavailable trophy devices with their standard variant
    'sets': {},
}

# Set once the WoT Plus check failed — the whole mod stays inert then.
_g_disabled = False

# Account currently loaded into CONFIG (0 = not known/loaded yet).
_g_account_id = 0


def account_id():
    """WoT account databaseID, or 0 if not known yet (e.g. mod init runs
    before login — wait for PlayerEvents.g_playerEvents.onAccountShowGUI)."""
    try:
        from gui.shared.utils import getPlayerDatabaseID
        return getPlayerDatabaseID() or 0
    except Exception:
        LOG.exc('mod_auto_equip.account_id failed')
        return 0


def account_files_dir():
    appdata = os.getenv('APPDATA', '')
    return os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'mods', 'z4imon', 'autoequipmentreturn')


def _config_path(acc_id):
    return os.path.join(account_files_dir(), '%s.json' % acc_id)


def _sanitize_set(raw):
    if not isinstance(raw, list):
        return None
    out = []
    for cd in raw:
        try:
            out.append(int(cd))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _sanitize_cd(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def load_config():
    """(Re)loads CONFIG for _g_account_id. Must only be called once that is
    a real account id (see _finish_account_load)."""
    path = _config_path(_g_account_id)
    try:
        if os.path.exists(path):
            with open(path, 'r') as fh:
                data = json.load(fh)
            CONFIG['autoEquipEnabled'] = bool(data.get('autoEquipEnabled', True))
            CONFIG['downgradeEnabled'] = bool(data.get('downgradeEnabled', False))
            sets = data.get('sets', {})
            clean = {}
            if isinstance(sets, dict):
                for key, entry in sets.iteritems():
                    if not isinstance(entry, dict):
                        continue
                    clean[str(key)] = {
                        'set1': _sanitize_set(entry.get('set1')),
                        'set2': _sanitize_set(entry.get('set2')),
                        'vehicleCD': _sanitize_cd(entry.get('vehicleCD')),
                    }
            CONFIG['sets'] = clean
        else:
            # First time this account is seen — start clean, then give
            # kurzdor's save for the same account id a chance to seed it.
            CONFIG['autoEquipEnabled'] = True
            CONFIG['downgradeEnabled'] = False
            CONFIG['sets'] = {}
            _try_kurzdor_first_run_import()
            save_config()
    except Exception:
        LOG.exc('load_config failed, keeping defaults')


def _try_kurzdor_first_run_import():
    try:
        import auto_equip_import
        auto_equip_import.auto_import_for_account(_g_account_id)
    except Exception:
        LOG.exc('kurzdor first-run auto-import failed')


def save_config():
    path = _config_path(_g_account_id)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        with open(path, 'w') as fh:
            json.dump({
                'autoEquipEnabled': bool(CONFIG['autoEquipEnabled']),
                'downgradeEnabled': bool(CONFIG['downgradeEnabled']),
                'sets': CONFIG['sets'],
            }, fh, indent=4)
    except Exception:
        LOG.exc('save_config failed')


# --------------------------------------------------------------------------
# Accessors
# --------------------------------------------------------------------------

def is_disabled():
    return _g_disabled


def set_disabled():
    global _g_disabled
    _g_disabled = True


def is_auto_enabled():
    return bool(CONFIG['autoEquipEnabled']) and not _g_disabled


def set_auto_enabled(value):
    CONFIG['autoEquipEnabled'] = bool(value)
    save_config()
    return CONFIG['autoEquipEnabled']


def is_downgrade_enabled():
    return bool(CONFIG['downgradeEnabled']) and not _g_disabled


def set_downgrade_enabled(value):
    CONFIG['downgradeEnabled'] = bool(value)
    save_config()
    return CONFIG['downgradeEnabled']


def get_sets(veh_inv_id):
    """The stored sets entry for a vehicle, or None if nothing is saved yet."""
    return CONFIG['sets'].get(str(veh_inv_id))


def store_sets(veh_inv_id, set1=None, set2=None, veh_cd=None):
    """Store (overwrite) the given set lists for a vehicle. Pass None to leave
    a set untouched. veh_cd (the vehicle's type compactDescr), when known,
    is recorded alongside for cross-account import remapping."""
    entry = CONFIG['sets'].setdefault(str(veh_inv_id), {'set1': None, 'set2': None, 'vehicleCD': None})
    entry.setdefault('vehicleCD', None)
    if set1 is not None:
        entry['set1'] = [int(cd) for cd in set1]
    if set2 is not None:
        entry['set2'] = [int(cd) for cd in set2]
    if veh_cd is not None:
        entry['vehicleCD'] = int(veh_cd)
    save_config()
    return entry


# --------------------------------------------------------------------------
# WoT lifecycle hooks
# --------------------------------------------------------------------------

def init():
    try:
        import auto_equip_i18n
        auto_equip_i18n.init()
        import auto_equip_gameface
        auto_equip_gameface.init()
        if os.name == 'nt':
            _start_account_load()
        LOG.info('mod_auto_equip.init() finished OK')
    except Exception:
        LOG.exc('mod_auto_equip.init() failed')


def _start_account_load():
    """Config loading (and the import panel it seeds) needs a real account
    id. init() runs before login, so wait for one if it's not ready yet —
    same event auto_equip_import used to wait for on its own."""
    acc_id = account_id()
    if acc_id:
        _finish_account_load(acc_id)
        return
    LOG.info('mod_auto_equip: account id not ready yet, waiting for onAccountShowGUI')
    from PlayerEvents import g_playerEvents
    g_playerEvents.onAccountShowGUI += _on_account_show_gui


def _on_account_show_gui(ctx=None):
    from PlayerEvents import g_playerEvents
    g_playerEvents.onAccountShowGUI -= _on_account_show_gui
    _finish_account_load(account_id())


def _finish_account_load(acc_id):
    global _g_account_id
    _g_account_id = acc_id
    load_config()
    try:
        import auto_equip_import
        auto_equip_import.register(acc_id)
    except Exception:
        LOG.exc('auto_equip_import.register failed')


def fini():
    try:
        import auto_equip_gameface
        auto_equip_gameface.fini()
    except Exception:
        LOG.exc('mod_auto_equip.fini() failed')
