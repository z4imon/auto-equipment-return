# -*- coding: utf-8 -*-
"""Snapshotting the current vehicle's setups into the config - the other half
of the feature apply.py restores."""

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
