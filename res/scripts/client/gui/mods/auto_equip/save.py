# -*- coding: utf-8 -*-
"""Snapshotting the current vehicle's setups into the config - the other half
of the feature apply.py restores - and throwing a snapshot away again."""

from . import config, inventory
from .i18n import t
from .log import LOG

from CurrentVehicle import g_currentVehicle

SET_1 = 1
SET_2 = 2
BOTH_SETS = 3

_STATUS_TEXT = {SET_1: 'set1Saved', SET_2: 'set2Saved', BOTH_SETS: 'bothSetsSaved'}


def save_current_vehicle_sets(which):
    """Stores the selected vehicle's current setups. `which` is SET_1, SET_2 or
    BOTH_SETS. Returns the status text to show the player."""
    vehicle = g_currentVehicle.item
    if vehicle is None:
        return t('noVehicleSelected')

    snapshot = inventory.snapshot_setups(vehicle)
    if which == SET_2 and snapshot['set2'] is None:
        return t('noSecondSetup')

    set1 = snapshot['set1'] if which in (SET_1, BOTH_SETS) else None
    set2 = snapshot['set2'] if which in (SET_2, BOTH_SETS) else None
    config.store_sets(vehicle.invID, set1=set1, set2=set2, veh_cd=vehicle.intCD)
    LOG.info('saved sets for %s (which=%s): set1=%s set2=%s'
             % (vehicle.userName, which, set1, set2))
    return t(_STATUS_TEXT.get(which, 'bothSetsSaved'))


def delete_current_vehicle_sets():
    """Drops the selected vehicle's saved sets. Returns True when something was
    actually deleted. Nothing is said to the player: the popover redraws right
    away and then reads "nothing saved yet", which is the answer."""
    vehicle = g_currentVehicle.item
    if vehicle is None:
        return False
    if not config.delete_sets(vehicle.invID):
        return False
    LOG.info('deleted the saved sets of %s' % vehicle.userName)
    return True


def pending_set_updates(vehicle):
    """{set name: the cd list it would have to become} for every set that IS
    stored for this vehicle but no longer matches what the vehicle carries.
    Empty means the stored sets are up to date.

    Deliberately narrower than save_current_vehicle_sets(): a set that was never
    stored stays unstored, and a vehicle with nothing stored has nothing pending.
    Storing a set is how the player opts a vehicle into this mod, and autosave
    (autosave.py, the only caller) must never make that choice for them."""
    saved = config.saved_sets(vehicle.invID)
    if saved is None:
        return {}

    snapshot = inventory.snapshot_setups(vehicle)
    updates = {}
    for key in ('set1', 'set2'):
        stored, current = saved.get(key), snapshot[key]
        if stored is None or current is None or list(stored) == list(current):
            continue
        updates[key] = current
    return updates


def update_already_saved_sets(vehicle):
    """Writes every pending update from pending_set_updates(). Returns the names
    of the sets that changed, so an empty list means nothing needed writing."""
    updates = pending_set_updates(vehicle)
    if not updates:
        return []
    config.store_sets(vehicle.invID, set1=updates.get('set1'),
                      set2=updates.get('set2'), veh_cd=vehicle.intCD)
    return sorted(updates)
