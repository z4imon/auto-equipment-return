import json
import os

from auto_equip_log import LOG

# --------------------------------------------------------------------------
# Persisted configuration (hand-editable plain JSON, per user preference)
#
# sets: { "<vehicle intCD>": { "set1": [intCD, intCD, intCD] or null,
#                              "set2": [intCD, intCD, intCD] or null } }
# A 0 inside a set list means "slot empty on purpose".
# --------------------------------------------------------------------------

CONFIG = {
    'autoEquipEnabled': True,   # global switch for the automatic install on vehicle selection
    'downgradeEnabled': False,  # replace unavailable trophy devices with their standard variant
    'sets': {},
}

# Set once the WoT Plus check failed — the whole mod stays inert then.
_g_disabled = False


def _config_path():
    appdata = os.getenv('APPDATA', '')
    return os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'mods', 'z4imon', 'AutoEquipReturn.json')


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


def load_config():
    path = _config_path()
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
                    }
            CONFIG['sets'] = clean
        else:
            save_config()
    except Exception:
        LOG.exc('load_config failed, keeping defaults')


def save_config():
    path = _config_path()
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


def get_sets(veh_cd):
    """The stored sets entry for a vehicle, or None if nothing is saved yet."""
    return CONFIG['sets'].get(str(veh_cd))


def store_sets(veh_cd, set1=None, set2=None):
    """Store (overwrite) the given set lists for a vehicle. Pass None to leave
    a set untouched."""
    entry = CONFIG['sets'].setdefault(str(veh_cd), {'set1': None, 'set2': None})
    if set1 is not None:
        entry['set1'] = [int(cd) for cd in set1]
    if set2 is not None:
        entry['set2'] = [int(cd) for cd in set2]
    save_config()
    return entry


# --------------------------------------------------------------------------
# WoT lifecycle hooks
# --------------------------------------------------------------------------

def init():
    try:
        if os.name == 'nt':
            load_config()
        import auto_equip_gameface
        auto_equip_gameface.init()
        LOG.info('mod_auto_equip.init() finished OK')
    except Exception:
        LOG.exc('mod_auto_equip.init() failed')


def fini():
    try:
        import auto_equip_gameface
        auto_equip_gameface.fini()
    except Exception:
        LOG.exc('mod_auto_equip.fini() failed')
