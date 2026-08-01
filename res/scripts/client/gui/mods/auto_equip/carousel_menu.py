# -*- coding: utf-8 -*-
"""An extra entry in the tank carousel's right-click menu that strips a vehicle
of all equipment which comes off for free.

That menu is the old Scaleform context menu, not a Gameface view: the client
builds it from plain dicts in VehicleContextMenuHandler._generateOptions and
routes clicks by option id through onOptionSelect. Both are hooked here, so
there is no markup and no resource file behind this button.

Hooking onOptionSelect rather than the handler map the constructor takes keeps
us clear of the name-mangled _AbstractContextMenuHandler__handlers.

MONEY GUARANTEE, same as apply.py: every device is checked against
inventory.is_free_to_demount() first, and anything that would cost credits,
gold or a demount kit stays mounted. The raw RPCs show no confirm dialog, so
that check is the only thing standing between the player and a silent charge.

The run is deliberately silent - no confirm dialog, no summary message. The
carousel redraws itself once the inventory comes back from the server.
"""

from adisp import adisp_async, adisp_process
from gui.Scaleform.daapi.view.lobby.hangar.hangar_cm_handlers import VehicleContextMenuHandler
from gui.shared.notifications import NotificationPriorityLevel

from . import config, inventory, messages, rpc
from . import apply as apply_engine
from .i18n import t
from .log import LOG

_OPTION_ID = 'z4imonDemountFree'

# One run at a time, and never on top of an apply run - both move the same
# devices around.
_busy = False


# ---------------------------------------------------------------------------
# The menu entry
# ---------------------------------------------------------------------------

def _free_devices(vehicle):
    """Every mounted device of this vehicle that can be removed for free, as
    gui items. A device sitting in both setups is one physical item and is
    listed once.

    There is no built-in variant to skip here: isBuiltIn belongs to Equipment
    (consumables and boosters), and OptionalDevice descends from
    RemovableDevice instead - a different branch entirely."""
    found = []
    seen = set()
    for setup_idx in inventory.setup_indices(vehicle):
        for device_cd in inventory.setup_device_cds(vehicle, setup_idx):
            if not device_cd or device_cd in seen:
                continue
            seen.add(device_cd)
            item = inventory.device_by_cd(device_cd)
            if item is not None and inventory.is_free_to_demount(item):
                found.append(item)
    return found


def _demount_option(handler):
    """Our menu item for the right-clicked vehicle, or None when the entry
    should not appear at all."""
    if config.is_mod_disabled() or not inventory.has_wot_plus():
        return None
    vehicle = inventory.vehicle_by_inv_id(handler.getVehInvID())
    if vehicle is None:
        return None
    enabled = (not vehicle.isLocked
               and not _busy
               and not apply_engine.is_busy()
               and bool(_free_devices(vehicle)))
    return handler._makeItem(_OPTION_ID, t('cmDemountFree'), {'enabled': enabled})


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

_original_generate_options = VehicleContextMenuHandler._generateOptions
_original_on_option_select = VehicleContextMenuHandler.onOptionSelect


def _hooked_generate_options(self, ctx=None):
    options = _original_generate_options(self, ctx)
    try:
        item = _demount_option(self)
        if item is not None:
            options.append(self._makeSeparator())
            options.append(item)
    except Exception:
        LOG.exc('could not add the demount entry to the carousel menu')
    return options


def _hooked_on_option_select(self, optionId):
    if optionId != _OPTION_ID:
        return _original_on_option_select(self, optionId)
    try:
        demount_free_equipment(self.getVehInvID())
    except Exception:
        LOG.exc('demount from the carousel menu failed')


def init():
    VehicleContextMenuHandler._generateOptions = _hooked_generate_options
    VehicleContextMenuHandler.onOptionSelect = _hooked_on_option_select


def fini():
    VehicleContextMenuHandler._generateOptions = _original_generate_options
    VehicleContextMenuHandler.onOptionSelect = _original_on_option_select


# ---------------------------------------------------------------------------
# The run
#
# Slot indices always address the ACTIVE setup, so a vehicle with two loadouts
# is walked one setup at a time and put back on the one it started on. Devices
# are demounted with allSetups=True, which pulls them out of both loadouts in a
# single call - the second pass therefore only ever finds devices that are
# unique to the second setup.
# ---------------------------------------------------------------------------

@adisp_process
def demount_free_equipment(veh_inv_id):
    global _busy
    if _busy or apply_engine.is_busy():
        LOG.info('carousel demount: another run is busy, ignoring')
        return
    vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
    if vehicle is None:
        return
    if vehicle.isLocked:
        LOG.warning('carousel demount: %s is locked, aborting' % veh_inv_id)
        return

    _busy = True
    original_setup_idx = inventory.active_setup_index(vehicle)
    removed = 0
    try:
        LOG.info('carousel demount: start for %s' % vehicle.userName)
        for setup_idx in inventory.setup_indices(vehicle):
            removed += yield _clear_setup(veh_inv_id, setup_idx)
        yield _restore_active_setup(veh_inv_id, original_setup_idx)
        LOG.info('carousel demount: done for %s - %d device(s) removed'
                 % (vehicle.userName, removed))
    except Exception:
        LOG.exc('demount_free_equipment failed')
    finally:
        _busy = False
        # Always reported, even at 0: the entry is only clickable when there IS
        # something free to remove, so a zero means something went wrong and
        # silence would just leave the player wondering whether the click took.
        # HIGH is what makes it pop up in the hangar instead of only landing in
        # the notification centre - same as the batch run's message.
        messages.push_info(t('cmDemountDone', count=removed, veh=vehicle.userName),
                           priority=NotificationPriorityLevel.HIGH)


@adisp_async
@adisp_process
def _clear_setup(veh_inv_id, setup_idx, callback=None):
    """Empties every free slot of ONE setup. Reports how many devices came off."""
    removed = 0
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        if not any(inventory.setup_device_cds(vehicle, setup_idx)):
            return      # already empty - no pointless setup switch

        if inventory.active_setup_index(vehicle) != setup_idx:
            code = yield rpc.change_setup_index(vehicle.invID, setup_idx)
            if not rpc.is_success(code):
                LOG.warning('carousel demount: setup %d not switchable (code %s)'
                            % (setup_idx + 1, code))
                return
            yield rpc.pause(rpc.OP_PAUSE)

        for slot_idx in range(inventory.slot_capacity(vehicle)):
            if inventory.vehicle_by_inv_id(veh_inv_id) is None:
                break
            if (yield _clear_slot(veh_inv_id, setup_idx, slot_idx)):
                removed += 1
    except Exception:
        LOG.exc('_clear_setup failed')
    finally:
        if callback is not None:
            callback(removed)


@adisp_async
@adisp_process
def _clear_slot(veh_inv_id, setup_idx, slot_idx, callback=None):
    """Removes the occupant of one slot, unless that would cost anything.
    Reports True only when a device actually came off."""
    removed = False
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        device_cd = inventory.setup_device_cds(vehicle, setup_idx)[slot_idx]
        if not device_cd:
            return
        item = inventory.device_by_cd(device_cd)
        if item is None:
            return
        if not inventory.is_free_to_demount(item):
            LOG.info('carousel demount: keeping %s - removal would not be free'
                     % item.userName)
            return

        code, extra = yield rpc.equip_device(vehicle.invID, 0, slot_idx,
                                             True, not item.isRemovable)
        if not rpc.is_success(code):
            LOG.warning('carousel demount: %s failed (code %s, %s)'
                        % (item.userName, code, extra))
            return
        removed = True
        yield rpc.pause(rpc.OP_PAUSE)
    except Exception:
        LOG.exc('_clear_slot failed')
    finally:
        if callback is not None:
            callback(removed)


@adisp_async
@adisp_process
def _restore_active_setup(veh_inv_id, original_setup_idx, callback=None):
    """Puts the vehicle back on the setup that was active before the run."""
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None or original_setup_idx is None:
            return
        if inventory.active_setup_index(vehicle) == original_setup_idx:
            return
        code = yield rpc.change_setup_index(vehicle.invID, original_setup_idx)
        if not rpc.is_success(code):
            LOG.warning('carousel demount: could not restore active setup %s on %s'
                        % (original_setup_idx, veh_inv_id))
    except Exception:
        LOG.exc('_restore_active_setup failed')
    finally:
        if callback is not None:
            callback(None)
