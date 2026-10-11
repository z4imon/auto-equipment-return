# -*- coding: utf-8 -*-
"""Mode sets: optional equipment of its own per vehicle for Frontline and for
Onslaught (design: docs/superpowers/specs/2026-10-10-mode-equipment-sets-design.md
in the outer repo).

Off by default (config.mode_sets_enabled()). While it is off, every function
here answers exactly what config.py answers for the normal sets, and no mode
file is read or written.

One file per profile, <account>.<profile>.json next to the account config.
The account importer only lists <digits>.json, so none of them shows up
there. Synced to the cloud on their own routes (sync.reconcile_modes),
separate from the normal sets.

Two profiles are in play at any time. The HANGAR profile follows the hangar
the player is in and decides what gets installed and where the equipment
window autosave writes. The VIEW profile is what the popover's switch shows;
it starts at the hangar profile on every hangar load and decides where the
Save buttons and the bin act."""

import json
import os
import time

from . import config, hangar
from .log import LOG

PROFILES = ('frontline', 'comp7')

# hangar.MODE_PACKAGES -> profile. Onslaught light shares Onslaught's sets.
_PROFILE_OF_MODE = {'frontline': 'frontline', 'comp7': 'comp7', 'comp7_light': 'comp7'}

# The tiers each mode is played at: the switch offers a profile only for these
# vehicles. Fixed here, not read from the client; a season that moves a mode to
# other tiers needs this table changed.
_PROFILE_LEVELS = {'frontline': (8,), 'comp7': (8, 10)}

_FILE_VERSION = 1

_account_id = 0
_sets = {}              # profile -> {str(invID): entry}, only profiles read so far
_unreadable = set()     # profiles whose file exists but could not be read: never written
_listeners = []         # callback(profile, invID) after a local store/delete

_UNSET = object()
_last_hangar_profile = _UNSET
_view_profile = _UNSET


def load_for_account(account_id):
    """Remembers the account. Files are read on first use, so a player who
    never turns the feature on never has them touched."""
    global _account_id, _sets, _unreadable, _view_profile, _last_hangar_profile
    _account_id = account_id
    _sets = {}
    _unreadable = set()
    _view_profile = _UNSET
    _last_hangar_profile = _UNSET


def enabled():
    return config.mode_sets_enabled()


def _path(profile):
    return os.path.join(config.account_files_dir(), '%s.%s.json' % (_account_id, profile))


def _cds(raw):
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError('not a list')
    return [int(cd or 0) for cd in raw]


def _clean_entry(raw):
    if not isinstance(raw, dict):
        return None
    try:
        entry = {
            'set1': _cds(raw.get('set1')),
            'set2': _cds(raw.get('set2')),
            'vehicleCD': int(raw['vehicleCD']) if raw.get('vehicleCD') is not None else None,
            'updatedAt': float(raw['updatedAt']) if raw.get('updatedAt') is not None else None,
            'deleted': bool(raw.get('deleted')),
        }
    except (TypeError, ValueError):
        return None
    if entry['deleted']:
        # A delete tombstone for the cloud sync (see delete()); without its
        # time it could never win a merge, so it is dropped.
        if entry['updatedAt'] is None:
            return None
        entry['set1'] = entry['set2'] = None
        return entry
    if entry['set1'] is None and entry['set2'] is None:
        return None
    return entry


def _profile_sets(profile):
    if profile in _sets:
        return _sets[profile]
    found = {}
    path = _path(profile)
    if os.path.exists(path):
        try:
            with open(path, 'r') as handle:
                data = json.load(handle)
            for inv_id, raw in (data.get('sets') or {}).items():
                entry = _clean_entry(raw)
                if entry is not None:
                    found[str(inv_id)] = entry
        except Exception:
            # Left as it is on disk: writing now would replace whatever the
            # player had there with the few sets saved since.
            LOG.exc('could not read %s, its sets stay empty and the file is left alone' % path)
            found = {}
            _unreadable.add(profile)
    _sets[profile] = found
    return found


def _save(profile):
    if profile in _unreadable or not _account_id:
        return
    try:
        config.write_json_atomic(_path(profile),
                                 {'version': _FILE_VERSION, 'sets': _sets.get(profile, {})})
    except Exception:
        LOG.exc('could not write %s' % _path(profile))


def hangar_profile():
    """The profile of the hangar the player is in, None for the normal sets."""
    if not enabled():
        return None
    return _PROFILE_OF_MODE.get(hangar.active_mode())


def _fits(profile, vehicle_level):
    """Whether a vehicle of this tier can have the profile's sets. None (the
    normal sets) always fits; an unknown tier is not held against it."""
    return profile is None or vehicle_level is None or vehicle_level in _PROFILE_LEVELS[profile]


def _level_of(veh_inv_id):
    """The tier of one owned vehicle, None when it cannot be looked up."""
    try:
        from . import inventory
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        return vehicle.level if vehicle is not None else None
    except Exception:
        LOG.exc('could not look up the tier of %s' % veh_inv_id)
        return None


def hangar_profile_for(vehicle_level):
    """The hangar profile as it applies to a vehicle of this tier: a tier 9
    in the Onslaught hangar gets the normal sets, Onslaught has no tier 9."""
    profile = hangar_profile()
    return profile if _fits(profile, vehicle_level) else None


def viewer_profile_for(vehicle_level):
    """The profile the view without WoT Plus asks streamers for: the hangar's
    mode at that tier, whatever the own-equipment switch says (user decision
    2026-10-12), else None for the normal set."""
    profile = _PROFILE_OF_MODE.get(hangar.active_mode())
    return profile if profile is not None and _fits(profile, vehicle_level) else None


def in_hangar_mode(vehicle_level):
    """Whether a vehicle of this tier can play the hangar's mode. Always True
    in the normal hangar and while the feature is off."""
    return _fits(hangar_profile(), vehicle_level)


def view_profile(vehicle_level=None):
    """The profile the popover shows and its Save buttons write to."""
    if not enabled():
        return None
    if _view_profile is _UNSET:
        return hangar_profile_for(vehicle_level)
    return _view_profile


def available_profiles(vehicle_level):
    """The profiles the switch offers for a vehicle of this tier, None (the
    normal sets) first: only the modes that exist at that tier. Empty while
    the feature is off."""
    if not enabled():
        return []
    return [None] + [profile for profile in PROFILES if _fits(profile, vehicle_level)]


def fit_view_profile(vehicle_level):
    """Puts the switch back to the hangar profile when it shows a profile the
    vehicle has no mode for, e.g. Frontline after picking a tier 10. Returns
    the view profile."""
    if view_profile(vehicle_level) not in available_profiles(vehicle_level):
        reset_view_profile()
    return view_profile(vehicle_level)


def select_view_profile(profile, vehicle_level=None):
    """profile '' or None picks the normal sets. False for anything unknown,
    for a profile the vehicle's tier has no mode for, or while the feature is
    off."""
    global _view_profile
    profile = profile or None
    if not enabled() or (profile is not None and profile not in PROFILES):
        return False
    if vehicle_level is not None and profile not in available_profiles(vehicle_level):
        return False
    _view_profile = profile
    return True


def reset_view_profile():
    global _view_profile
    _view_profile = _UNSET


def is_foreign_view(vehicle_level=None):
    return view_profile(vehicle_level) != hangar_profile_for(vehicle_level)


def own_sets(veh_inv_id, profile):
    """The profile's own entry. None for a vehicle whose tier the mode does not
    have, even if an entry exists: the normal sets apply there. A delete
    tombstone counts as no entry."""
    if profile is None or not enabled():
        return None
    entry = _profile_sets(profile).get(str(veh_inv_id))
    if entry is None or entry.get('deleted') or not _fits(profile, _level_of(veh_inv_id)):
        return None
    return entry


def sets_for(veh_inv_id, profile):
    """The profile's own entry, else exactly config.saved_sets()."""
    own = own_sets(veh_inv_id, profile)
    if own is not None:
        return own
    return config.saved_sets(veh_inv_id)


def effective_sets(veh_inv_id):
    """What installs: the hangar profile's sets."""
    return sets_for(veh_inv_id, hangar_profile())


def has_effective_sets(veh_inv_id):
    if own_sets(veh_inv_id, hangar_profile()) is not None:
        return True
    return config.has_saved_sets(veh_inv_id)


def has_target_sets(veh_inv_id, profile):
    """Whether the sets a save into this profile would replace already exist.
    In a mode profile the normal sets only show as fallback."""
    if profile is None or not enabled():
        return config.has_saved_sets(veh_inv_id)
    return own_sets(veh_inv_id, profile) is not None


def store(veh_inv_id, profile, set1=None, set2=None, veh_cd=None):
    """config.store_sets() for the normal sets. A mode profile's first write
    seeds the entry with a copy of the normal sets, so saving only set 1 does
    not leave set 2 empty. A vehicle whose tier the mode does not have
    stores into the normal sets, which are what it uses there."""
    if profile is None or not enabled() or not _fits(profile, _level_of(veh_inv_id)):
        return config.store_sets(veh_inv_id, set1=set1, set2=set2, veh_cd=veh_cd)
    sets = _profile_sets(profile)
    entry = sets.get(str(veh_inv_id))
    if entry is None or entry.get('deleted'):
        base = config.saved_sets(veh_inv_id) if config.has_saved_sets(veh_inv_id) else None
        entry = {
            'set1': list(base['set1']) if base and base.get('set1') is not None else None,
            'set2': list(base['set2']) if base and base.get('set2') is not None else None,
            'vehicleCD': base.get('vehicleCD') if base else None,
            'updatedAt': None,
            'deleted': False,
        }
        sets[str(veh_inv_id)] = entry
    if set1 is not None:
        entry['set1'] = [int(cd) for cd in set1]
    if set2 is not None:
        entry['set2'] = [int(cd) for cd in set2]
    if veh_cd is not None:
        entry['vehicleCD'] = int(veh_cd)
    entry['updatedAt'] = time.time()
    entry['deleted'] = False
    _save(profile)
    LOG.info('%s sets stored for %s' % (profile, veh_inv_id))
    _notify(profile, veh_inv_id)
    return entry


def delete(veh_inv_id, profile):
    """config.delete_sets() for the normal sets. In a mode profile the entry
    becomes a delete tombstone, so the cloud sync does not bring it back,
    and the normal sets apply there again."""
    if profile is None or not enabled():
        return config.delete_sets(veh_inv_id)
    sets = _profile_sets(profile)
    entry = sets.get(str(veh_inv_id))
    if entry is None or entry.get('deleted'):
        return False
    sets[str(veh_inv_id)] = {'set1': None, 'set2': None, 'vehicleCD': entry.get('vehicleCD'),
                             'updatedAt': time.time(), 'deleted': True}
    _save(profile)
    LOG.info('%s sets deleted for %s' % (profile, veh_inv_id))
    _notify(profile, veh_inv_id)
    return True


def all_mode_sets(veh_inv_id):
    """Every mode entry of the vehicle, for the code that has to respect mode
    equipment in any hangar: donor order, cleanup, statistics. Empty while
    the feature is off."""
    if not enabled():
        return []
    return [entry for entry in (own_sets(veh_inv_id, profile) for profile in PROFILES)
            if entry is not None]


def slot_mismatches(current, wanted, extras):
    """Indices of occupied slots whose device matches neither the wanted
    layout nor any of the extra layouts at that slot. With no extras this is
    exactly the per-slot test cleanup always did."""
    result = []
    for slot_idx, device_cd in enumerate(current):
        if not device_cd or device_cd == wanted[slot_idx]:
            continue
        if any(slot_idx < len(extra) and device_cd == extra[slot_idx] for extra in extras):
            continue
        result.append(slot_idx)
    return result


def note_hangar_profile():
    """Called on every hangar load. Resets the popover's switch to the new
    hangar and returns True when the hangar profile differs from the previous
    load's. The first load is never a change; with the feature off the
    profile is always None, so there is never one either."""
    global _last_hangar_profile
    current = hangar_profile()
    changed = enabled() and _last_hangar_profile is not _UNSET and current != _last_hangar_profile
    _last_hangar_profile = current
    reset_view_profile()
    return changed


# ---------------------------------------------------------------------------
# Cloud sync support (sync.py). The merge writes through apply_remote_entry,
# which notifies nobody, so nothing it applies is pushed straight back.
# ---------------------------------------------------------------------------

def add_change_listener(callback):
    """callback(profile, veh_inv_id) after every local store or delete in a
    mode profile; veh_inv_id is a string."""
    _listeners.append(callback)


def _notify(profile, veh_inv_id):
    for callback in list(_listeners):
        try:
            callback(profile, str(veh_inv_id))
        except Exception:
            LOG.exc('a mode sets change listener failed')


def syncable(profile):
    """Whether the sync may read and write this profile: feature on, a known
    profile, an account, and a readable file."""
    if not enabled() or profile not in PROFILES or not _account_id:
        return False
    _profile_sets(profile)
    return profile not in _unreadable


def all_inv_ids(profile):
    """Every stored invID of the profile, tombstones included."""
    return list(_profile_sets(profile).keys())


def raw_entry(profile, veh_inv_id):
    """The entry as stored: tombstones and vehicles of other tiers included."""
    return _profile_sets(profile).get(str(veh_inv_id))


def apply_remote_entry(profile, veh_inv_id, set1, set2, veh_cd, updated_at, deleted=False):
    """The server's entry, as it is, for sync.py's merge."""
    deleted = bool(deleted)
    _profile_sets(profile)[str(veh_inv_id)] = {
        'set1': [int(cd or 0) for cd in set1] if set1 is not None and not deleted else None,
        'set2': [int(cd or 0) for cd in set2] if set2 is not None and not deleted else None,
        'vehicleCD': int(veh_cd) if veh_cd is not None else None,
        'updatedAt': float(updated_at),
        'deleted': deleted,
    }
    _save(profile)


def set_updated_at(profile, veh_inv_id, updated_at):
    """The server's time for an entry after a push; content unchanged."""
    entry = _profile_sets(profile).get(str(veh_inv_id))
    if entry is None:
        return
    entry['updatedAt'] = float(updated_at)
    _save(profile)
