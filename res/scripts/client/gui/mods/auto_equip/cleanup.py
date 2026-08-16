# -*- coding: utf-8 -*-
"""Garage-wide cleanup, one vehicle at a time:

    is a set saved for it?  ->  does what is mounted differ from it?  ->  take
    off whatever is not in its saved place and comes off for free.

WHAT COUNTS AS MISPLACED is decided per SLOT: the device in slot N is misplaced
unless it is exactly the device the saved layout puts in slot N. A device that
is in the saved set but in the wrong slot therefore comes off too - it is not
where it was saved. Empty slots are never a problem, and a slot the saved set
leaves empty must be empty.

The comparison is against the RAW saved set (apply.build_plan), never the
downgraded one - and that is the whole point of this feature. A standard device
the install run fitted because the bounty one could not be sourced is not what
the saved set names for that slot, so it comes off here and goes back to the
depot, where the player can put it on something else.

Vehicles with nothing saved are never touched: with no layout to compare
against, every device on them would look misplaced. Locked vehicles and
mode-locked loaners drop out for the same reasons they do everywhere else.

MONEY GUARANTEE, same as apply.py and carousel_menu.py: a device only comes off
when inventory.is_free_to_demount() says so. Anything else stays mounted and is
named in the summary. The raw RPCs show no confirm dialog, so that check is the
only thing between the player and a silent charge.
"""

from adisp import adisp_async, adisp_process

from . import config, inventory, messages, rpc
from . import apply as apply_engine
from .i18n import t
from .log import LOG

# Dropdown option order in the settings panel - the index arrives as the
# button's value, so these ARE the wire format.
SCOPE_ALL, SCOPE_PRIMARY = 0, 1

_MAX_SUMMARY_ERRORS = 8

# Device names run long, and the summary is a system message, not a report.
_MAX_SUMMARY_ITEMS = 10

# One run at a time, and never on top of an apply run - both move the same
# devices around.
_busy = False


def is_busy():
    return _busy


def _other_run_busy():
    """The apply engine and the carousel's demount entry move the same devices
    around, so neither may overlap with this run.

    carousel_menu is imported inside the function on purpose: it imports THIS
    module at load time, and closing that cycle at module level would leave
    whichever of the two loaded first holding a half-built module."""
    if apply_engine.is_busy():
        return True
    try:
        from . import carousel_menu
        return carousel_menu.is_busy()
    except Exception:
        LOG.exc('could not read the carousel demount state')
        return False


# ---------------------------------------------------------------------------
# Planning (no server calls)
# ---------------------------------------------------------------------------

def targets(scope):
    """The vehicles this run will look at, best tier first.

    SCOPE_PRIMARY narrows to the Primary vehicles of the hangar the player is
    in - the same set the batch install run works on, which is where downgrades
    come from in the first place."""
    if scope == SCOPE_PRIMARY:
        vehicles = inventory.filtered_primary_vehicles()
    else:
        vehicles = inventory.owned_vehicles()
    found = [vehicle for vehicle in vehicles if _is_target(vehicle)]
    return sorted(found, key=lambda v: (-v.level, v.userName))


def _is_target(vehicle):
    try:
        if not config.has_saved_sets(vehicle.invID):
            return False
        if inventory.is_mode_only_vehicle(vehicle):
            return False
        return not vehicle.isLocked
    except Exception:
        LOG.exc('could not decide whether to clean up a vehicle')
        return False


def has_misplaced(vehicle):
    """Whether this vehicle is worth a server call at all.

    misplaced_slots() reads the items cache and nothing else, so scanning the
    whole garage up front is free - and it is what lets an already tidy garage
    say "nothing to do" instead of showing a veil over a run that touches
    nothing."""
    try:
        saved = config.saved_sets(vehicle.invID)
        return saved is not None and bool(misplaced_slots(vehicle, saved))
    except Exception:
        LOG.exc('could not scan a vehicle for misplaced equipment')
        return False


def misplaced_slots(vehicle, saved):
    """[(setup index, [slot indices])] - every occupied slot carrying something
    other than what the saved layout puts there.

    The test is per SLOT, not per set: a device that is in the saved set but in
    the wrong slot counts as misplaced and comes off, because the slot it sits
    in is not the one it was saved for. Setups that were never saved are not
    part of the plan and keep whatever they are carrying."""
    plan = []
    for setup_idx, wanted in apply_engine.build_plan(vehicle, saved):
        current = inventory.setup_device_cds(vehicle, setup_idx)
        slots = [slot_idx for slot_idx, device_cd in enumerate(current)
                 if device_cd and device_cd != wanted[slot_idx]]
        if slots:
            plan.append((setup_idx, slots))
    return plan


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

class _Totals(object):
    """What the whole run did, in the shape the summary needs."""

    def __init__(self):
        self.removed = 0            # successful demount operations
        self.vehicles = 0           # vehicles that lost at least one device
        self.to_depot = {}          # device name -> how many reached the depot
        self.kept = {}              # device name -> left on because it costs
        self.errors = []            # ready-made, user-facing lines

    def note_removed(self, device_name, to_depot):
        self.removed += 1
        if to_depot:
            self.to_depot[device_name] = self.to_depot.get(device_name, 0) + 1

    def note_kept(self, device_name):
        self.kept[device_name] = self.kept.get(device_name, 0) + 1

    def note_vehicle(self, removed):
        if removed:
            self.vehicles += 1

    def note_error(self, vehicle, line):
        self.errors.append(t('batchVehicleError', veh=vehicle.userName, err=line))

    def summary_lines(self):
        """A run that found nothing is one line; everything else leads with the
        count and then says where the devices went."""
        if not (self.removed or self.kept or self.errors):
            return [t('cleanupNothing')]
        lines = [t('cleanupDone', count=self.removed, vehicles=self.vehicles)]
        if self.to_depot:
            lines.append(t('cleanupToDepot', items=_item_list(self.to_depot)))
        if self.kept:
            # Named, not just counted: this is the money guarantee doing its
            # job, and the player can only act on it if they know what stayed.
            lines.append(t('cleanupKept', items=_item_list(self.kept)))
        for error in self.errors[:_MAX_SUMMARY_ERRORS]:
            lines.append(t('summaryError', err=error))
        if len(self.errors) > _MAX_SUMMARY_ERRORS:
            lines.append(t('batchMoreErrors',
                           count=len(self.errors) - _MAX_SUMMARY_ERRORS))
        return lines


def _item_list(counts):
    """{name: count} -> "2x Rammer, 1x Optik", cut off with a count of the
    rest rather than silently truncated."""
    names = sorted(counts)
    shown = [t('batchMissingLine', count=counts[name], name=name)
             for name in names[:_MAX_SUMMARY_ITEMS]]
    if len(names) > _MAX_SUMMARY_ITEMS:
        shown.append(t('cleanupMoreItems', count=len(names) - _MAX_SUMMARY_ITEMS))
    return u', '.join(shown)


# ---------------------------------------------------------------------------
# The run
#
# Slot indices always address the ACTIVE setup, so a vehicle with two loadouts
# is walked one setup at a time and put back on the one it started on.
# ---------------------------------------------------------------------------

@adisp_process
def demount_misplaced(scope=SCOPE_ALL):
    global _busy
    if _busy or _other_run_busy():
        messages.push_warning(t('alreadyRunning'))
        return

    candidates = targets(scope)
    found = [vehicle for vehicle in candidates if has_misplaced(vehicle)]
    if not found:
        LOG.info('cleanup: nothing to do (%d vehicle(s) with saved sets scanned)'
                 % len(candidates))
        messages.push_warning(t('cleanupNothing'))
        return

    # Auto-install is deliberately left as it is. The batch run turns it off to
    # protect a fleet it just equipped; here the opposite is wanted - once the
    # standard devices are back in the depot the normal per-vehicle install is
    # exactly what should redistribute them.
    _busy = True
    totals = _Totals()
    # Same veil wording as the carousel's demount entry - it is the same job,
    # just over the whole garage.
    veil_shown = messages.show_waiting(messages.WAITING_KEY_OPERATION,
                                       t('cmDemountWaiting'))
    try:
        LOG.info('cleanup: start, scope=%s, %d of %d vehicle(s) need work: %s'
                 % (scope, len(found), len(candidates),
                    [vehicle.userName for vehicle in found]))
        for vehicle in found:
            yield _clean_vehicle(vehicle.invID, totals)
        messages.push_lines(totals.summary_lines(),
                            warning=bool(totals.errors or totals.kept))
        LOG.info('cleanup: done - %d device(s) off %d vehicle(s), '
                 'to depot=%s, kept=%s, errors=%d'
                 % (totals.removed, totals.vehicles, totals.to_depot,
                    totals.kept, len(totals.errors)))
    except Exception:
        LOG.exc('demount_misplaced failed')
    finally:
        _busy = False
        if veil_shown:
            messages.hide_waiting(messages.WAITING_KEY_OPERATION)
        # The popover is showing slots this run just emptied behind its back.
        apply_engine.notify_refresh()


@adisp_async
@adisp_process
def _clean_vehicle(veh_inv_id, totals, callback=None):
    """Strips one vehicle of everything its saved sets do not name."""
    removed = 0
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        saved = config.saved_sets(veh_inv_id)
        if vehicle is None or saved is None:
            return
        plan = misplaced_slots(vehicle, saved)
        if not plan:
            return

        LOG.info('cleanup: %s has %d misplaced slot(s)'
                 % (vehicle.userName, sum(len(slots) for _, slots in plan)))
        original_setup_idx = inventory.active_setup_index(vehicle)
        for setup_idx, slots in plan:
            if inventory.vehicle_by_inv_id(veh_inv_id) is None:
                break
            removed += yield _clean_setup(veh_inv_id, setup_idx, slots, totals)
        yield apply_engine.restore_active_setup(veh_inv_id, original_setup_idx)
    except Exception:
        LOG.exc('_clean_vehicle failed')
    finally:
        totals.note_vehicle(removed)
        if callback is not None:
            callback(None)


@adisp_async
@adisp_process
def _clean_setup(veh_inv_id, setup_idx, slots, totals, callback=None):
    """Empties the listed slots of ONE setup. Reports how many devices came
    off."""
    removed = 0
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return

        # Demounts always address the ACTIVE setup, so switch there first.
        if inventory.active_setup_index(vehicle) != setup_idx:
            code = yield rpc.change_setup_index(vehicle.invID, setup_idx)
            if not rpc.is_success(code):
                totals.note_error(vehicle, t('errSetupSwitchFailed',
                                             setup=setup_idx + 1, code=code))
                return
            yield rpc.pause(rpc.OP_PAUSE)

        for slot_idx in slots:
            if inventory.vehicle_by_inv_id(veh_inv_id) is None:
                break
            if (yield _clean_slot(veh_inv_id, setup_idx, slot_idx, totals)):
                removed += 1
    except Exception:
        LOG.exc('_clean_setup failed')
    finally:
        if callback is not None:
            callback(removed)


@adisp_async
@adisp_process
def _clean_slot(veh_inv_id, setup_idx, slot_idx, totals, callback=None):
    """Takes the occupant of one slot off, unless that would cost something.
    Reports True only when a device actually came off.

    The slot is re-read here rather than trusted from the plan: a device
    mounted in BOTH setups is pulled out of the first one by the pass before,
    and the plan does not know that happened."""
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
            totals.note_error(vehicle, t('errUnknownItem', slot=slot_idx + 1,
                                         cd=device_cd))
            return

        other_setups = [idx for idx in inventory.setup_indices(vehicle)
                        if idx != setup_idx]
        stays_on_vehicle = any(
            inventory.vehicle_has_device(vehicle, device_cd, setup_idx=idx)
            for idx in other_setups)
        if stays_on_vehicle:
            # Nothing leaves the vehicle, so there is nothing to pay for - and
            # nothing reaches the depot either, which is why this is counted
            # separately from the depot list.
            code, extra = yield rpc.equip_device(vehicle.invID, 0, slot_idx,
                                                 False, False)
        else:
            if not inventory.is_free_to_demount(item):
                totals.note_kept(item.userName)
                LOG.info('cleanup: keeping %s on %s - removal would not be free'
                         % (item.userName, vehicle.userName))
                return
            code, extra = yield rpc.equip_device(vehicle.invID, 0, slot_idx,
                                                 True, not item.isRemovable)
        if not rpc.is_success(code):
            totals.note_error(vehicle, t('errDemountFailed', name=item.userName,
                                         code=code, ext=extra))
            return

        totals.note_removed(item.userName, to_depot=not stays_on_vehicle)
        removed = True
        yield rpc.pause(rpc.OP_PAUSE)
    except Exception:
        LOG.exc('_clean_slot failed')
    finally:
        if callback is not None:
            callback(removed)
