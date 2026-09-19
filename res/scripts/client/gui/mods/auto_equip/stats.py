# -*- coding: utf-8 -*-
"""How much of this realm's target equipment the player has saved, and how
much of it he actually owns.

The question this answers is the one the mod could not answer before: is the
depot deep enough for what has been saved? Until now a shortage only surfaced
as "skipped" lines after an equip run.

Read-only, like inventory.py, and nothing is cached: gui items are recreated on
every cache sync, so every call re-reads.

COUNTING RULES (user, 2026-09-19). Both columns count VEHICLES, which is what
makes the difference between them readable:

    saved(device) = vehicles in scope whose saved sets name the device
    owned(device) = depot count + vehicles carrying the device

Both deduplicate per vehicle. A device named in set 1 AND set 2 is one device -
the client remounts it when the player switches loadouts, it does not need two.
The same holds for a device mounted in setup 0 and setup 1.

This rounds DOWN by design. Devices are identified by intCD, which names the
TYPE, not the individual item, so two physically separate red lvl 2 rammers are
indistinguishable here. Showing a gap that is not there beats hiding one.
"""

from . import config, inventory
from .log import LOG

ALL = 'all'
PRIMARY = 'primary'
PLAYLIST = 'playlist'

# Both loadouts a vehicle can hold - see apply.PRIMARY_SETUP (0).
_SETUPS = (0, 1)


def _saved_device_cds(veh_inv_id):
    """The distinct devices one vehicle has saved, across BOTH sets."""
    entry = config.saved_sets(veh_inv_id)
    if not entry:
        return set()
    found = set()
    for key in ('set1', 'set2'):
        for device_cd in (entry.get(key) or []):
            if device_cd:
                found.add(int(device_cd))
    return found


def _scope_vehicles(scope):
    """The vehicles a scope counts - already reduced to those with a saved
    set, since a vehicle the mod knows nothing about cannot demand equipment.

    The two batch scopes mirror their equip buttons exactly, mode loaners
    included: apply.equip_primary_vehicles() and equip_playlist_vehicles()
    drop those the same way, and a statistic that disagreed with the button
    next to it would be worse than no statistic."""
    try:
        if scope == PRIMARY:
            candidates = inventory.filtered_primary_vehicles()
        elif scope == PLAYLIST:
            candidates, _missing = inventory.playlist_vehicles()
        elif scope == ALL:
            candidates = inventory.owned_vehicles()
        else:
            LOG.warning('unknown stats scope %r' % (scope,))
            return []
        return [vehicle for vehicle in candidates
                if not inventory.is_mode_only_vehicle(vehicle)
                and config.has_saved_sets(vehicle.invID)]
    except Exception:
        LOG.exc('_scope_vehicles(%r) failed' % (scope,))
        return []


def _saved_counts(vehicles):
    counts = {}
    for vehicle in vehicles:
        for device_cd in _saved_device_cds(vehicle.invID):
            counts[device_cd] = counts.get(device_cd, 0) + 1
    return counts


def _mounted_counts():
    """How many VEHICLES carry each device, whole garage, scope-independent.

    Both setups are read, not just the active one: a device parked in the
    loadout the player is not currently using is still owned, and leaving it
    out would report a shortage the player does not have."""
    counts = {}
    try:
        for vehicle in inventory.owned_vehicles():
            on_this_vehicle = set()
            for setup_idx in _SETUPS:
                for device_cd in (inventory.setup_device_cds(vehicle, setup_idx) or []):
                    if device_cd:
                        on_this_vehicle.add(int(device_cd))
            for device_cd in on_this_vehicle:
                counts[device_cd] = counts.get(device_cd, 0) + 1
    except Exception:
        LOG.exc('_mounted_counts failed')
    return counts


def _depot_count(item):
    try:
        return int(item.inventoryCount or 0)
    except Exception:
        return 0


def collect(scope):
    """{'scope', 'vehicleCount', 'rows': [{'cd', 'saved', 'owned'}, ...]}

    Rows are this realm's whole target tier, including devices nobody has
    saved - a zero is information too ("I own four of these and use none").
    gameface.py turns each row into the payload the view draws.

    Any failure degrades to zeroed rows rather than raising: a wrong number
    here must never take the popover down with it."""
    vehicles = _scope_vehicles(scope)
    saved = _saved_counts(vehicles)
    mounted = _mounted_counts()
    rows = []
    for item in inventory.target_devices():
        try:
            device_cd = int(item.intCD)
        except Exception:
            continue
        rows.append({'cd': device_cd,
                     'saved': saved.get(device_cd, 0),
                     'owned': _depot_count(item) + mounted.get(device_cd, 0)})
    # Most-saved first: that is where a shortage costs the most, so it belongs
    # at the top rather than wherever the alphabet put it. The sort is stable
    # and keys on saved ALONE, so devices tied on that keep the name order
    # inventory.target_devices() already put them in.
    rows.sort(key=lambda row: -row['saved'])
    return {'scope': scope, 'vehicleCount': len(vehicles), 'rows': rows}
