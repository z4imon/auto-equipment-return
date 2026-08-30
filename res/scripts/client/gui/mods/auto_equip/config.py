# -*- coding: utf-8 -*-
"""Persisted settings and saved equipment sets, one JSON file per account.

    <the client's preferences folder>/mods/z4imon/autoequipmentreturn/<accountId>.json

(the same folder shape kurzdor's mod uses), so switching between accounts on
the same PC never mixes up saved sets. File layout:

    {"autoEquipEnabled": true,
     "downgradeEnabled": false,
     "alwaysSelectSetup1": true,
     "sets": {"<vehicle invID>": {"set1": [intCD, intCD, intCD] or null,
                                  "set2": [intCD, intCD, intCD] or null,
                                  "vehicleCD": intCD or null}}}

Sets are keyed by the vehicle's INVENTORY id, not by its type compactDescr, so
data imported from kurzdor's Auto Equipment Return mod - which uses the same
scheme - lines up without translation. vehicleCD (the vehicle's type
compactDescr) is recorded alongside purely so a set can later be remapped onto
a DIFFERENT account's own invID for that vehicle type; an invID is only
meaningful within the account that assigned it (see importer.py).
A 0 inside a set list means "slot empty on purpose".

Plain, hand-editable JSON with real booleans, on purpose.
"""

import json
import os

from helpers import getPreferencesDirPath

from .log import LOG

_DEFAULTS = {
    'autoEquipEnabled': True,   # install saved sets automatically on vehicle selection
    'downgradeEnabled': False,  # replace unavailable special devices with their standard variant
    # Leave every vehicle on set 1 and never switch a donor back. Off restores
    # the old behaviour: donors return to their own setup and the vehicle ends
    # on whichever setup it started on.
    'alwaysSelectSetup1': True,
}

_EMPTY_ENTRY = {'set1': None, 'set2': None, 'vehicleCD': None}

_settings = dict(_DEFAULTS)
_sets = {}

# Account whose file is currently loaded (0 = none yet).
_account_id = 0

# Set once the WoT Plus check failed - the whole mod stays inert from then on.
_mod_disabled = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def mods_dir():
    return os.path.join(getPreferencesDirPath(), 'mods')


def account_files_dir():
    return os.path.join(mods_dir(), 'z4imon', 'autoequipmentreturn')


def _config_path(account_id):
    return os.path.join(account_files_dir(), '%s.json' % account_id)


# ---------------------------------------------------------------------------
# Loading and saving
# ---------------------------------------------------------------------------

def _as_int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _clean_set(raw):
    """A stored set list -> list of ints, unreadable entries becoming 0
    ("empty slot"). None stays None, which means "this set was never saved"."""
    if not isinstance(raw, list):
        return None
    return [_as_int_or_none(cd) or 0 for cd in raw]


def _clean_entry(raw):
    return {
        'set1': _clean_set(raw.get('set1')),
        'set2': _clean_set(raw.get('set2')),
        'vehicleCD': _as_int_or_none(raw.get('vehicleCD')),
    }


def _clean_sets(raw):
    if not isinstance(raw, dict):
        return {}
    return dict((str(key), _clean_entry(entry))
                for key, entry in raw.iteritems()
                if isinstance(entry, dict))


def load_for_account(account_id):
    """(Re)loads the config for `account_id`. Must only be called once that is
    a real account id - see mod_auto_equip's account-load sequence."""
    global _account_id, _settings, _sets
    _account_id = account_id
    path = _config_path(account_id)
    try:
        if os.path.exists(path):
            with open(path, 'r') as handle:
                data = json.load(handle)
            _settings = {
                'autoEquipEnabled': bool(data.get('autoEquipEnabled', True)),
                'downgradeEnabled': bool(data.get('downgradeEnabled', False)),
                'alwaysSelectSetup1': bool(data.get('alwaysSelectSetup1', True)),
            }
            _sets = _clean_sets(data.get('sets', {}))
        else:
            # First time we see this account: start clean, then give kurzdor's
            # save for the same account id a chance to seed it.
            _settings = dict(_DEFAULTS)
            _sets = {}
            _import_kurzdor_save_once()
            save()
    except Exception:
        LOG.exc('load_for_account(%s) failed, keeping defaults' % account_id)


def _import_kurzdor_save_once():
    # Imported lazily: importer reads this module at import time.
    try:
        from . import importer
        importer.auto_import_for_account(_account_id)
    except Exception:
        LOG.exc('kurzdor first-run auto-import failed')


def save():
    path = _config_path(_account_id)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            json.dump({
                'autoEquipEnabled': bool(_settings['autoEquipEnabled']),
                'downgradeEnabled': bool(_settings['downgradeEnabled']),
                'alwaysSelectSetup1': bool(_settings['alwaysSelectSetup1']),
                'sets': _sets,
            }, handle, indent=4)
    except Exception:
        LOG.exc('save() failed')


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def is_mod_disabled():
    return _mod_disabled


def disable_mod():
    global _mod_disabled
    _mod_disabled = True


def is_auto_enabled():
    return bool(_settings['autoEquipEnabled']) and not _mod_disabled


def set_auto_enabled(enabled):
    _settings['autoEquipEnabled'] = bool(enabled)
    save()
    return _settings['autoEquipEnabled']


def is_downgrade_enabled():
    return bool(_settings['downgradeEnabled']) and not _mod_disabled


def set_downgrade_enabled(enabled):
    _settings['downgradeEnabled'] = bool(enabled)
    save()
    return _settings['downgradeEnabled']


def is_always_setup1():
    """Whether a run leaves the vehicle on set 1 and skips the donor's switch
    back. Both halves hang on this one flag because they are the same trade:
    fewer CMD_SWITCH_LAYOUT calls in exchange for vehicles ending on a setup
    the player did not pick.

    Not gated on _mod_disabled: a disabled mod performs no runs, so there is no
    setup to leave anyone on."""
    return bool(_settings.get('alwaysSelectSetup1', True))


def set_always_setup1(enabled):
    _settings['alwaysSelectSetup1'] = bool(enabled)
    save()
    return _settings['alwaysSelectSetup1']


# ---------------------------------------------------------------------------
# Saved sets
# ---------------------------------------------------------------------------

def saved_sets(veh_inv_id):
    """The stored entry for a vehicle, or None if nothing is saved yet."""
    return _sets.get(str(veh_inv_id))


def has_saved_sets(veh_inv_id):
    entry = saved_sets(veh_inv_id)
    return entry is not None and (entry['set1'] is not None or entry['set2'] is not None)


def store_sets(veh_inv_id, set1=None, set2=None, veh_cd=None):
    """Stores (overwrites) the given set lists for a vehicle. Pass None to
    leave a set untouched. veh_cd, when known, is recorded alongside for later
    cross-account import remapping."""
    entry = _sets.setdefault(str(veh_inv_id), dict(_EMPTY_ENTRY))
    entry.setdefault('vehicleCD', None)
    if set1 is not None:
        entry['set1'] = [int(cd) for cd in set1]
    if set2 is not None:
        entry['set2'] = [int(cd) for cd in set2]
    if veh_cd is not None:
        entry['vehicleCD'] = int(veh_cd)
    save()
    return entry


def delete_sets(veh_inv_id):
    """Forgets everything stored for a vehicle - both sets and the vehicleCD.
    Returns True when there was something to forget."""
    if _sets.pop(str(veh_inv_id), None) is None:
        return False
    save()
    return True


def fill_in_vehicle_cd(veh_inv_id, veh_cd):
    """Fills in vehicleCD on an already-saved entry that predates that field
    (kurzdor imports carry no vehicle type id - see importer.py).
    Returns True only if a value was actually written."""
    entry = saved_sets(veh_inv_id)
    if entry is None or entry.get('vehicleCD'):
        return False
    entry['vehicleCD'] = int(veh_cd)
    save()
    return True