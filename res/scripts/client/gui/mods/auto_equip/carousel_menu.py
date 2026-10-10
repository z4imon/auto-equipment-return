# -*- coding: utf-8 -*-
"""Extra entries in the tank carousel's right-click menu:

  * Demount equipment          - one vehicle, every device that comes off free
  * Demount bounty equipment   - one vehicle, trophy/bounty devices only

Garage-wide bounty demount lives on the hangar popover (gameface.py), not here.

That menu is the old Scaleform context menu, not a Gameface view: the client
builds it from plain dicts in VehicleContextMenuHandler._generateOptions and
routes clicks by option id through onOptionSelect. Both are hooked here, so
there is no markup and no resource file behind these buttons.

Hooking onOptionSelect rather than the handler map the constructor takes keeps
us clear of the name-mangled _AbstractContextMenuHandler__handlers.

MONEY GUARANTEE, same as apply.py: every device is checked against
inventory.is_free_to_demount() first, and anything that would cost credits,
gold or a demount kit stays mounted. The raw RPCs show no confirm dialog, so
that check is the only thing standing between the player and a silent charge.

There is no confirm dialog: nothing here can cost anything, so a click is not
worth guarding. A run shows the hangar veil while it works and reports how
many devices came off afterwards.
"""

from adisp import adisp_async, adisp_process
from gui.Scaleform.daapi.view.lobby.hangar.hangar_cm_handlers import VehicleContextMenuHandler
from gui.shared.notifications import NotificationPriorityLevel

from . import cleanup, config, inventory, messages, rpc
from . import apply as apply_engine
from .i18n import t
from .log import LOG

_OPTION_ID = 'z4imonDemountFree'
_OPTION_DEMOUNT_TROPHY = 'z4imonDemountAllTrophy'

# One run at a time, and never on top of an apply or cleanup run - all three
# move the same devices around.
_busy = False
# The veil text of the running run, so the hangar popover's busy row says
# "demounting" instead of its default "installing".
_busy_text = None


def is_busy():
    return _busy


def busy_text():
    return _busy_text if _busy else None


def _other_run_busy():
    return apply_engine.is_busy() or cleanup.is_busy()


def _accept_all(_item):
    return True


def _is_trophy(item):
    try:
        return bool(item.isTrophy)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The menu entries
# ---------------------------------------------------------------------------

def _matching_devices(vehicle, accept):
    """Mounted devices on this vehicle that pass `accept` and come off for
    free. A device sitting in both setups is one physical item and is listed
    once.

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
            if item is None or not accept(item):
                continue
            if inventory.is_free_to_demount(item):
                found.append(item)
    return found


def _free_devices(vehicle):
    return _matching_devices(vehicle, _accept_all)


def _trophy_vehicles():
    """Owned tanks that currently carry a free-to-demount trophy device."""
    found = []
    for vehicle in inventory.owned_vehicles():
        try:
            if vehicle.isLocked or inventory.is_mode_only_vehicle(vehicle):
                continue
            if _matching_devices(vehicle, _is_trophy):
                found.append(vehicle)
        except Exception:
            LOG.exc('could not scan %s for trophy equipment'
                    % getattr(vehicle, 'userName', '?'))
    return sorted(found, key=lambda v: (-v.level, v.userName))


def _runs_idle():
    return not _busy and not _other_run_busy()


def _cm_label(key):
    """Scaleform context-menu labels must be UTF-8 byte strings.

    i18n.t() returns unicode. ASCII labels (the original demount entry) happen
    to survive either way; a German 'ü' in Erbeutete Ausrüstung does not - the
    Flash menu drops that row and keeps the rest."""
    text = t(key)
    if isinstance(text, unicode):
        try:
            return text.encode('utf-8')
        except Exception:
            LOG.exc('could not encode context-menu label %s' % key)
            return text
    return text


def _demount_option(handler):
    """Our menu item for the right-clicked vehicle, or None when the entry
    should not appear at all."""
    if (config.is_mod_disabled()
            or not config.is_carousel_menu_row_enabled('demountEquip')
            or not inventory.has_wot_plus()):
        return None
    vehicle = inventory.vehicle_by_inv_id(handler.getVehInvID())
    if vehicle is None:
        return None
    if inventory.is_mode_only_vehicle(vehicle):
        # Hidden, not greyed out: nothing the player could do would ever make
        # this work, so a disabled row would only invite them to wonder why.
        return None
    enabled = (not vehicle.isLocked
               and _runs_idle()
               and bool(_free_devices(vehicle)))
    return handler._makeItem(_OPTION_ID, _cm_label('cmDemountFree'),
                             {'enabled': enabled})


def _demount_all_trophy_option(handler):
    """Bounty strip for the right-clicked vehicle only. The hangar popover
    is what walks the whole garage (demount_all_trophy)."""
    if (config.is_mod_disabled()
            or not config.is_carousel_menu_row_enabled('demountBounty')
            or not inventory.has_wot_plus()):
        return None
    vehicle = inventory.vehicle_by_inv_id(handler.getVehInvID())
    if vehicle is None:
        return None
    if inventory.is_mode_only_vehicle(vehicle):
        return None
    enabled = (not vehicle.isLocked
               and _runs_idle()
               and bool(_matching_devices(vehicle, _is_trophy)))
    return handler._makeItem(_OPTION_DEMOUNT_TROPHY, _cm_label('cmDemountTrophy'),
                             {'enabled': enabled})


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

_original_generate_options = VehicleContextMenuHandler._generateOptions
_original_on_option_select = VehicleContextMenuHandler.onOptionSelect


def _hooked_generate_options(self, ctx=None):
    options = _original_generate_options(self, ctx)
    extras = []
    for builder in (_demount_option, _demount_all_trophy_option):
        try:
            item = builder(self)
            if item is not None:
                extras.append(item)
        except Exception:
            LOG.exc('could not build carousel menu entry %s' % builder.__name__)
    if extras:
        options.append(self._makeSeparator())
        options.extend(extras)
    return options


def _hooked_on_option_select(self, optionId):
    if optionId == _OPTION_ID:
        try:
            demount_free_equipment(self.getVehInvID())
        except Exception:
            LOG.exc('demount from the carousel menu failed')
        return
    if optionId == _OPTION_DEMOUNT_TROPHY:
        try:
            demount_trophy_equipment(self.getVehInvID())
        except Exception:
            LOG.exc('bounty demount from the carousel menu failed')
        return
    return _original_on_option_select(self, optionId)


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
    global _busy, _busy_text
    if not config.is_carousel_menu_row_enabled('demountEquip'):
        LOG.info('carousel demount: feature toggle off, ignoring')
        return
    if _busy or _other_run_busy():
        LOG.info('carousel demount: another run is busy, ignoring')
        return
    vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
    if vehicle is None:
        return
    if vehicle.isLocked:
        LOG.warning('carousel demount: %s is locked, aborting' % veh_inv_id)
        return
    if inventory.is_mode_only_vehicle(vehicle):
        LOG.warning('carousel demount: %s is a mode-only loaner, aborting'
                    % vehicle.userName)
        return

    _busy = True
    _busy_text = t('cmDemountWaiting')
    removed = 0
    veil_shown = messages.show_waiting(messages.WAITING_KEY_OPERATION,
                                       _busy_text)
    try:
        LOG.info('carousel demount: start for %s' % vehicle.userName)
        removed = yield _demount_vehicle(veh_inv_id, _accept_all)
        LOG.info('carousel demount: done for %s - %d device(s) removed'
                 % (vehicle.userName, removed))
    except Exception:
        LOG.exc('demount_free_equipment failed')
    finally:
        _busy = False
        if veil_shown:
            messages.hide_waiting(messages.WAITING_KEY_OPERATION)
        # Always reported, even at 0: the entry is only clickable when there IS
        # something free to remove, so a zero means something went wrong and
        # silence would just leave the player wondering whether the click took.
        # HIGH is what makes it pop up in the hangar instead of only landing in
        # the notification centre - same as the batch run's message.
        messages.push_info(t('cmDemountDone', count=removed, veh=vehicle.userName),
                           priority=NotificationPriorityLevel.HIGH)
        apply_engine.notify_refresh()


@adisp_process
def demount_trophy_equipment(veh_inv_id):
    """Takes free-to-demount trophy/bounty devices off ONE vehicle."""
    global _busy, _busy_text
    if not config.is_carousel_menu_row_enabled('demountBounty'):
        LOG.info('carousel bounty demount: feature toggle off, ignoring')
        return
    if _busy or _other_run_busy():
        LOG.info('carousel bounty demount: another run is busy, ignoring')
        return
    vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
    if vehicle is None:
        return
    if vehicle.isLocked:
        LOG.warning('carousel bounty demount: %s is locked, aborting' % veh_inv_id)
        return
    if inventory.is_mode_only_vehicle(vehicle):
        LOG.warning('carousel bounty demount: %s is a mode-only loaner, aborting'
                    % vehicle.userName)
        return

    _busy = True
    _busy_text = t('demountAllTrophyWaiting')
    removed = 0
    veil_shown = messages.show_waiting(messages.WAITING_KEY_OPERATION,
                                       _busy_text)
    try:
        LOG.info('carousel bounty demount: start for %s' % vehicle.userName)
        removed = yield _demount_vehicle(veh_inv_id, _is_trophy)
        LOG.info('carousel bounty demount: done for %s - %d device(s) removed'
                 % (vehicle.userName, removed))
    except Exception:
        LOG.exc('demount_trophy_equipment failed')
    finally:
        _busy = False
        if veil_shown:
            messages.hide_waiting(messages.WAITING_KEY_OPERATION)
        messages.push_info(t('cmDemountDone', count=removed, veh=vehicle.userName),
                           priority=NotificationPriorityLevel.HIGH)
        apply_engine.notify_refresh()


@adisp_process
def demount_all_trophy():
    """Takes every free-to-demount trophy/bounty device off every eligible
    tank and leaves standard, Improved and Experimental devices mounted."""
    global _busy, _busy_text
    if not config.is_icon_menu_row_enabled('demountBounty'):
        LOG.info('carousel trophy demount: feature toggle off, ignoring')
        return
    if _busy or _other_run_busy():
        LOG.info('carousel trophy demount: another run is busy, ignoring')
        return

    found = _trophy_vehicles()
    if not found:
        messages.push_warning(t('demountAllTrophyNothing'))
        return

    _busy = True
    _busy_text = t('demountAllTrophyWaiting')
    removed = 0
    vehicles_touched = 0
    veil_shown = messages.show_waiting(messages.WAITING_KEY_OPERATION,
                                       _busy_text)
    try:
        LOG.info('carousel trophy demount: start, %d vehicle(s): %s'
                 % (len(found), [vehicle.userName for vehicle in found]))
        for vehicle in found:
            count = yield _demount_vehicle(vehicle.invID, _is_trophy)
            if count:
                vehicles_touched += 1
            removed += count
        LOG.info('carousel trophy demount: done - %d device(s) off %d vehicle(s)'
                 % (removed, vehicles_touched))
        messages.push_info(t('demountAllTrophyDone',
                             count=removed, vehicles=vehicles_touched),
                           priority=NotificationPriorityLevel.HIGH)
        # Auto-install would put the saved trophy sets straight back on the
        # next vehicle click. Same protection as the Primary batch run.
        if removed:
            apply_engine.disable_auto_install_after_batch()
    except Exception:
        LOG.exc('demount_all_trophy failed')
    finally:
        _busy = False
        if veil_shown:
            messages.hide_waiting(messages.WAITING_KEY_OPERATION)
        apply_engine.notify_refresh()


@adisp_async
@adisp_process
def _demount_vehicle(veh_inv_id, accept, callback=None):
    """Walks one vehicle's setups and reports how many devices came off."""
    removed = 0
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        original_setup_idx = inventory.active_setup_index(vehicle)
        for setup_idx in inventory.setup_indices(vehicle):
            if inventory.vehicle_by_inv_id(veh_inv_id) is None:
                break
            removed += yield _clear_setup(veh_inv_id, setup_idx, accept)
        yield _restore_active_setup(veh_inv_id, original_setup_idx)
    except Exception:
        LOG.exc('_demount_vehicle failed')
    finally:
        if callback is not None:
            callback(removed)


@adisp_async
@adisp_process
def _clear_setup(veh_inv_id, setup_idx, accept, callback=None):
    """Empties matching free slots of ONE setup. Reports how many devices came
    off."""
    removed = 0
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        if not _setup_has_match(vehicle, setup_idx, accept):
            return      # already empty of what we want - no pointless switch

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
            if (yield _clear_slot(veh_inv_id, setup_idx, slot_idx, accept)):
                removed += 1
    except Exception:
        LOG.exc('_clear_setup failed')
    finally:
        if callback is not None:
            callback(removed)


def _setup_has_match(vehicle, setup_idx, accept):
    for device_cd in inventory.setup_device_cds(vehicle, setup_idx):
        if not device_cd:
            continue
        item = inventory.device_by_cd(device_cd)
        if item is None or not accept(item):
            continue
        if inventory.is_free_to_demount(item):
            return True
    return False


@adisp_async
@adisp_process
def _clear_slot(veh_inv_id, setup_idx, slot_idx, accept, callback=None):
    """Removes the occupant of one slot, unless that would cost anything or
    the occupant is not wanted. Reports True only when a device actually came
    off."""
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
        if not accept(item):
            return
        if not inventory.is_free_to_demount(item):
            LOG.info('carousel demount: keeping %s - removal would not be free'
                     % item.userName)
            return

        code, extra = yield rpc.equip_device(
            vehicle.invID, 0, slot_idx, True, not item.isRemovable)
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
