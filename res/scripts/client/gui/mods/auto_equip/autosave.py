# -*- coding: utf-8 -*-
"""Keeping the saved sets in step with what the player actually mounts.

The popover's "Save set" rows are the explicit way to store a loadout, but a
player who rebuilds their equipment and goes straight into battle has already
told us which loadout they want back - and nobody remembers to press Save first.
So the mounted loadout is written back on its own at the two moments where the
editing is demonstrably over:

  * the player takes the vehicle into battle. The queue confirmation the server
    sends back (PlayerEvents.onEnqueued) still arrives while the hangar, the
    items cache and the vehicle selection are all alive, which the arena events
    no longer do. A platoon leader's BATTLE sends no such confirmation to the
    members, so the vehicle LOCK is watched as well: queue, platoon and arena all
    set it.
  * the selection leaves the vehicle for another one.

WHICH VEHICLES: only those whose stored sets and mounted equipment were in
agreement the last time we looked - see _recheck(). That is the whole safety
mechanism, because "the equipment differs from the saved set" has two completely
different causes:

  * the player just changed it -> exactly what should be saved,
  * the set was never realised on the vehicle in the first place (auto-install
    off, or an install run that could not source the bounty device) -> saving now
    would delete a loadout the player is still waiting for.

Telling them apart needs a moment where both were known to agree. So a vehicle
becomes eligible when its sets match what it carries - on selection, after an
install run, after a manual save - and any divergence from there on is the
player's own edit. A vehicle that was never in sync is never autosaved, which is
also why a vehicle with no stored sets stays untouched: storing a set is how the
player hands a vehicle to this mod, and autosave must not make that choice for
them.
"""

from CurrentVehicle import g_currentVehicle
from PlayerEvents import g_playerEvents

from . import config, inventory, save
from .log import LOG

# Fired once the server has accepted the queue request - i.e. the battle button
# actually did something. Read through getattr so a client that renames it
# degrades to "no autosave on the battle button" instead of breaking the mod.
_QUEUE_EVENT = 'onEnqueued'

# Vehicles whose stored sets were in sync with their equipment when we last
# looked, and which may therefore be autosaved. inv id -> True.
_in_sync = {}

# The vehicle the selection is on, and whether it was locked last time we
# looked; a fresh lock is what a platoon leader's BATTLE produces on our side.
_watched_inv_id = None
_watched_locked = False

_subscribed = False

# Set by the UI so the popover can redraw once a set was rewritten.
_refresh_callback = None

# Set by the mod so autosave can tell whether an install run is in flight,
# without importing apply.py (which imports this module).
_busy_probe = None


def set_refresh_callback(callback):
    global _refresh_callback
    _refresh_callback = callback


def set_busy_probe(probe):
    global _busy_probe
    _busy_probe = probe


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def init():
    """Starts listening for the battle button. Idempotent - the caller runs on
    every hangar load."""
    global _subscribed
    event = getattr(g_playerEvents, _QUEUE_EVENT, None)
    if event is None:
        LOG.warning('PlayerEvents.%s not available - autosave has to rely on the '
                    'vehicle lock alone' % _QUEUE_EVENT)
        return
    try:
        try:
            event -= _on_battle_queued
        except Exception:
            pass    # not currently subscribed
        event += _on_battle_queued
        if not _subscribed:
            LOG.info('subscribed to PlayerEvents.%s' % _QUEUE_EVENT)
        _subscribed = True
    except Exception:
        LOG.exc('failed to subscribe to PlayerEvents.%s' % _QUEUE_EVENT)


def fini():
    global _subscribed, _watched_inv_id, _watched_locked
    _watched_inv_id = None
    _watched_locked = False
    if not _subscribed:
        return
    try:
        event = getattr(g_playerEvents, _QUEUE_EVENT, None)
        if event is not None:
            event -= _on_battle_queued
    except Exception:
        LOG.exc('failed to unsubscribe from PlayerEvents.%s' % _QUEUE_EVENT)
    finally:
        _subscribed = False


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _on_battle_queued(*_args, **_kwargs):
    try:
        vehicle = g_currentVehicle.item
        if vehicle is None:
            return
        sync_vehicle(vehicle.invID, 'battle button')
    except Exception:
        LOG.exc('_on_battle_queued failed')


def on_vehicle_changed():
    """Hooked to g_currentVehicle.onChanged, ahead of the apply engine's own
    handler. Two things are interesting here: the selection moving on (the
    vehicle being left behind gets its changes written, and the new one is
    checked for eligibility), and the selected vehicle becoming locked (it just
    went into a queue, a platoon or a battle, so its loadout is final).

    onChanged also fires for every cache resync - including the ones our own
    install run causes - hence the "was it locked before" edge check and the busy
    guard in sync_vehicle()."""
    global _watched_inv_id, _watched_locked
    try:
        vehicle = g_currentVehicle.item
        inv_id = vehicle.invID if vehicle is not None else None
        locked = bool(vehicle.isLocked) if vehicle is not None else False

        if inv_id != _watched_inv_id:
            if _watched_inv_id is not None:
                sync_vehicle(_watched_inv_id, 'selection moved to another vehicle')
            if inv_id is not None:
                recheck(inv_id, 'selected')
        elif inv_id is not None and locked and not _watched_locked:
            sync_vehicle(inv_id, 'vehicle locked (queue, platoon or battle)')

        _watched_inv_id = inv_id
        _watched_locked = locked
    except Exception:
        LOG.exc('autosave.on_vehicle_changed failed')


def recheck_current_vehicle(context):
    """Same as recheck(), for whatever is selected right now - which is what the
    popover's own buttons act on."""
    try:
        vehicle = g_currentVehicle.item
        if vehicle is not None:
            recheck(vehicle.invID, context)
    except Exception:
        LOG.exc('autosave.recheck_current_vehicle failed')


def recheck(veh_inv_id, context):
    """Re-decides whether this vehicle may be autosaved: it may exactly while
    its stored sets and its mounted equipment agree (see the module docstring).

    Every part of the mod that moves equipment or rewrites a set has to report
    here afterwards - an install run, a save or delete in the popover, the
    carousel's demount entry. Otherwise a vehicle the MOD just changed still
    looks like one the PLAYER changed, and the next battle button would save the
    mod's own half-finished state over the player's set."""
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        in_sync = (vehicle is not None
                   and config.has_saved_sets(veh_inv_id)
                   and not save.pending_set_updates(vehicle))
        was_in_sync = _in_sync.pop(veh_inv_id, False)
        if in_sync:
            _in_sync[veh_inv_id] = True
        if in_sync != was_in_sync:
            LOG.info('autosave: %s %s autosaved from now on (%s)'
                     % (veh_inv_id, 'may be' if in_sync else 'may NOT be', context))
    except Exception:
        LOG.exc('autosave.recheck(%s) failed' % veh_inv_id)


# ---------------------------------------------------------------------------
# The write itself
# ---------------------------------------------------------------------------

def sync_vehicle(veh_inv_id, reason):
    """Writes the vehicle's mounted loadout back into its saved sets. Returns
    True only when something actually changed."""
    try:
        if config.is_mod_disabled() or not config.is_auto_save_enabled():
            return False
        if _is_busy():
            return False
        if veh_inv_id not in _in_sync:
            return False
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return False

        changed = save.update_already_saved_sets(vehicle)
        if not changed:
            return False
        LOG.info('autosave (%s): %s of %s updated to %s'
                 % (reason, ', '.join(changed), vehicle.userName,
                    inventory.snapshot_setups(vehicle)))
        _notify_refresh()
        return True
    except Exception:
        LOG.exc('autosave.sync_vehicle(%s) failed' % veh_inv_id)
        return False


def _is_busy():
    try:
        return bool(_busy_probe is not None and _busy_probe())
    except Exception:
        LOG.exc('autosave busy probe failed')
        return True     # do not write while the truth is unknown


def _notify_refresh():
    if _refresh_callback is None:
        return
    try:
        _refresh_callback()
    except Exception:
        LOG.exc('autosave refresh callback failed')
