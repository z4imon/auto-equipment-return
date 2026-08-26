# -*- coding: utf-8 -*-
"""Restoring saved equipment sets onto vehicles.

MONEY GUARANTEE: no operation in this module ever spends gold, credits or a
demount kit. Every demount that would cost something is skipped and reported
instead, and no device is installed unless it was verifiably free to obtain.
The raw RPCs in rpc.py show no confirm dialogs, so a missing check
here would charge the player silently.

One run per vehicle works through three phases per setup:

    1. clear the slots whose occupant has to go,
    2. make every wanted device available (depot, this vehicle, or a free
       demount from a donor vehicle),
    3. apply the whole layout in a single server command.

The steps that talk to the server are separate adisp async functions so the
run reads top-down; everything else is plain, synchronous and testable by eye.
"""

import BigWorld

from adisp import adisp_async, adisp_process
from CurrentVehicle import g_currentVehicle
from gui.shared.notifications import NotificationPriorityLevel

from . import config, inventory, messages, metrics, rpc
from .i18n import t
from .log import LOG

_MAX_SUMMARY_ERRORS = 8

# One apply run at a time - the popover reflects this, and the vehicle-
# selection trigger skips while it is set.
_busy = False

# Last vehicle the selection trigger reacted to; dedupes the onChanged storms
# a cache resync produces.
_last_inv_id = None

# Set by the UI so the popover can redraw after anything changed.
_refresh_callback = None


def is_busy():
    return _busy


def set_refresh_callback(callback):
    global _refresh_callback
    _refresh_callback = callback


def _notify_refresh():
    if _refresh_callback is None:
        return
    try:
        _refresh_callback()
    except Exception:
        LOG.exc('refresh callback failed')


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

class RunOptions(object):
    """The handful of knobs that differ between the single-vehicle run and the
    batch run over all Primary vehicles."""

    def __init__(self, watch_selection=True, force_downgrade=False,
                 show_veil=True, push_summary=True, excluded_donor_inv_ids=None):
        self.watch_selection = watch_selection
        self.force_downgrade = force_downgrade
        self.show_veil = show_veil
        self.push_summary = push_summary
        self.excluded_donor_inv_ids = excluded_donor_inv_ids


class RunOutcome(object):
    """What one vehicle's run ended up doing. The batch run aggregates these."""

    def __init__(self):
        self.installed = 0
        self.skipped = []       # (device name, reason)
        self.errors = []        # ready-made, user-facing error lines
        self.donated = []       # (device name, donor vehicle name)
        self.downgraded = []    # (special device name, standard device name)
        self.missing = []       # devices that could not be sourced for free
        # True when the run gave up before working through the whole plan -
        # the player selected another vehicle, or the vehicle vanished. Its
        # timings describe a partial run and must be readable as such.
        self.interrupted = False

    def has_anything_to_report(self):
        return bool(self.installed or self.skipped or self.errors or self.donated)

    def note_skipped(self, device_name, reason, also_missing=True):
        self.skipped.append((device_name, reason))
        if also_missing:
            self.missing.append(device_name)

    def note_downgrade(self, special_name, standard_name):
        note = (special_name, standard_name)
        if note not in self.downgraded:
            self.downgraded.append(note)


class DepotLedger(object):
    """Depot stock per device for ONE run, immune to items-cache resync lag.

    Seeded from a fresh read per device, then kept up to date by our OWN
    demounts and installs. Re-reading the cache right after a demount we just
    performed can still report the OLD count - the server ack has landed but
    the resync push hasn't caught up - which used to make the money guard
    throw away a perfectly successful donor demount as "not available"."""

    def __init__(self):
        self._counts = {}

    def count(self, device_cd):
        if device_cd not in self._counts:
            item = inventory.device_by_cd(device_cd)
            self._counts[device_cd] = item.inventoryCount if item is not None else 0
        return self._counts[device_cd]

    def freed_one(self, device_cd):
        self._counts[device_cd] = self.count(device_cd) + 1

    def took_one(self, device_cd):
        self._counts[device_cd] = self.count(device_cd) - 1


# ---------------------------------------------------------------------------
# Planning (no server calls)
# ---------------------------------------------------------------------------

def _normalize_layout(saved_cds, capacity):
    """A saved cd list -> a per-slot layout of exactly `capacity` entries."""
    cds = [int(cd) if cd else 0 for cd in list(saved_cds)[:capacity]]
    return cds + [0] * (capacity - len(cds))


def _build_plan(vehicle, saved):
    """[(setup index, wanted layout)] for the setups this vehicle actually
    has. Sets that were never saved are simply not part of the plan."""
    capacity = inventory.slot_capacity(vehicle)
    available = inventory.setup_indices(vehicle)
    wanted_per_setup = ((0, saved.get('set1')), (1, saved.get('set2')))
    return [(setup_idx, _normalize_layout(saved_cds, capacity))
            for setup_idx, saved_cds in wanted_per_setup
            if saved_cds is not None and setup_idx in available]


def _plan_already_applied(vehicle, plan):
    return all(inventory.setup_device_cds(vehicle, setup_idx) == wanted
               for setup_idx, wanted in plan)


def _downgraded_plan(plan, vehicle, veh_inv_id, options, outcome):
    """Swaps special devices (trophy, bounty/modernized, deluxe) that cannot be
    sourced for free with a cheaper counterpart - but only when THAT one is free
    to obtain itself, otherwise the normal skip flow reports the special device.
    inventory.downgrade_candidates_of() orders the alternatives by how strong
    they actually are in the slot, so an upgraded bounty device falls back to
    the standard device first and only then to the plain bounty one.

    This runs BEFORE the no-op check on purpose: with the standard variant
    already mounted the resolved plan matches the vehicle and nothing happens,
    where otherwise every selection would demount and remount it."""
    return [(setup_idx, _downgrade_layout(wanted, vehicle, veh_inv_id, options, outcome))
            for setup_idx, wanted in plan]


def _downgrade_layout(wanted, vehicle, veh_inv_id, options, outcome):
    resolved = list(wanted)
    for slot_idx, device_cd in enumerate(wanted):
        if not device_cd:
            continue
        item = inventory.device_by_cd(device_cd)
        if item is None or getattr(item, 'isRegular', True):
            continue
        if _is_free_to_obtain(vehicle, veh_inv_id, item, options):
            continue
        # Strongest first, which is not the same as closest: the standard
        # device is slot-boosted, a level 1 bounty device is not.
        for replacement in inventory.downgrade_candidates_of(vehicle, item):
            if replacement.intCD in resolved:
                continue
            if not _is_free_to_obtain(vehicle, veh_inv_id, replacement, options):
                continue
            resolved[slot_idx] = replacement.intCD
            outcome.note_downgrade(item.userName, replacement.userName)
            break
    return resolved


def _is_free_to_obtain(vehicle, veh_inv_id, item, options):
    """True if the device can be sourced without spending anything: already on
    this vehicle, sitting in the depot, or free-demountable from a donor."""
    try:
        if inventory.vehicle_has_device(vehicle, item.intCD):
            return True
        if item.inventoryCount > 0:
            return True
        if not inventory.is_free_to_demount(item):
            return False
        return inventory.find_donor_vehicle(
            item.intCD, veh_inv_id, options.excluded_donor_inv_ids) is not None
    except Exception:
        LOG.exc('_is_free_to_obtain failed')
        return False


# ---------------------------------------------------------------------------
# One vehicle
# ---------------------------------------------------------------------------

def _selection_moved_away(veh_inv_id):
    try:
        item = g_currentVehicle.item
        return item is None or item.invID != veh_inv_id
    except Exception:
        return True


def _run_interrupted(veh_inv_id, options):
    return options.watch_selection and _selection_moved_away(veh_inv_id)


@adisp_async
@adisp_process
def apply_to_vehicle(veh_inv_id, options, callback=None):
    """Restores both saved sets onto one vehicle. Every failure is recorded and
    the run carries on with the next slot; the outcome is reported through the
    callback as a RunOutcome."""
    outcome = RunOutcome()
    veil_shown = False
    try:
        saved = config.saved_sets(veh_inv_id)
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if saved is None or vehicle is None:
            return
        if vehicle.isLocked:
            LOG.warning('apply: vehicle %s is locked, aborting' % veh_inv_id)
            return
        if inventory.is_mode_only_vehicle(vehicle):
            # A mode loaner arrives fully equipped and the server will not let
            # any of it go, so a run here can only demount-fail its way through
            # the whole plan.
            LOG.info('apply: %s is a mode-only loaner, leaving its equipment alone'
                     % vehicle.userName)
            return

        metrics.set_vehicle(vehicle)
        original_setup_idx = inventory.active_setup_index(vehicle)
        plan = _build_plan(vehicle, saved)
        if not plan:
            return
        if options.force_downgrade or config.is_downgrade_enabled():
            plan = _downgraded_plan(plan, vehicle, veh_inv_id, options, outcome)
        if _plan_already_applied(vehicle, plan):
            return      # nothing to do - no veil flicker on every selection

        LOG.info('apply: start for %s, plan=%s' % (vehicle.userName, plan))
        if options.show_veil:
            veil_shown = messages.show_waiting()

        ledger = DepotLedger()
        for setup_idx, wanted in plan:
            if _run_interrupted(veh_inv_id, options):
                outcome.interrupted = True
                break
            keep_going = yield _apply_setup(veh_inv_id, setup_idx, wanted,
                                            options, outcome, ledger)
            if not keep_going:
                outcome.interrupted = True
                break

        yield _restore_active_setup(veh_inv_id, original_setup_idx)

        if options.push_summary and outcome.has_anything_to_report():
            messages.push_lines(_summary_lines(outcome),
                                warning=bool(outcome.skipped or outcome.errors))
        LOG.info('apply: done for %s - installed=%d skipped=%d errors=%d'
                 % (veh_inv_id, outcome.installed, len(outcome.skipped), len(outcome.errors)))
    except Exception:
        LOG.exc('apply_to_vehicle failed')
    finally:
        if veil_shown:
            messages.hide_waiting()
        if callback is not None:
            callback(outcome)


@adisp_async
@adisp_process
def _apply_setup(veh_inv_id, setup_idx, wanted, options, outcome, ledger, callback=None):
    """Brings ONE setup to `wanted`. Reports False when the whole run should
    stop, i.e. the player selected a different vehicle or the vehicle vanished."""
    keep_going = True
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            keep_going = False
            return
        if inventory.setup_device_cds(vehicle, setup_idx) == wanted:
            return

        # Installs always land in the ACTIVE setup, so switch there first.
        if inventory.active_setup_index(vehicle) != setup_idx:
            code = yield rpc.change_setup_index(vehicle.invID, setup_idx)
            if not rpc.is_success(code):
                outcome.errors.append(t('errSetupSwitchFailed', setup=setup_idx + 1, code=code))
                return
            yield rpc.pause(rpc.OP_PAUSE)

        # `final` is what phase 3 will actually apply: slots we cannot serve
        # fall back to their current content, or stay empty.
        final = list(wanted)
        capacity = inventory.slot_capacity(vehicle)
        for slot_idx in range(capacity):
            if _run_interrupted(veh_inv_id, options):
                break
            if inventory.vehicle_by_inv_id(veh_inv_id) is None:
                break
            yield _prepare_slot(veh_inv_id, setup_idx, slot_idx, wanted, final,
                                options, outcome, ledger)

        if _run_interrupted(veh_inv_id, options):
            keep_going = False
            return
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            keep_going = False
            return
        yield _install_layout(vehicle, setup_idx, final, capacity, outcome, ledger)
    except Exception:
        LOG.exc('_apply_setup failed')
    finally:
        if callback is not None:
            callback(keep_going)


@adisp_async
@adisp_process
def _prepare_slot(veh_inv_id, setup_idx, slot_idx, wanted, final,
                  options, outcome, ledger, callback=None):
    """Phases 1 and 2 for a single slot: free it if its occupant has to leave,
    then make sure the wanted device is obtainable. Adjusts `final` in place."""
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        current_cd = inventory.setup_device_cds(vehicle, setup_idx)[slot_idx]
        wanted_cd = final[slot_idx]
        if current_cd == wanted_cd:
            return

        if current_cd and current_cd not in wanted:
            cleared = yield _clear_slot(vehicle, setup_idx, slot_idx, current_cd,
                                        outcome, ledger)
            if not cleared:
                final[slot_idx] = current_cd
                return

        if not wanted_cd:
            return

        wanted_item = inventory.device_by_cd(wanted_cd)
        if wanted_item is None:
            outcome.errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=wanted_cd))
            final[slot_idx] = 0
            return

        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        already_here = inventory.vehicle_has_device(vehicle, wanted_cd)
        if already_here or ledger.count(wanted_cd) > 0:
            return

        obtained = yield _borrow_from_donor(veh_inv_id, wanted_cd, wanted_item,
                                            options, outcome, ledger)
        if not obtained:
            final[slot_idx] = 0
    except Exception:
        LOG.exc('_prepare_slot failed')
    finally:
        if callback is not None:
            callback(None)


@adisp_async
@adisp_process
def _clear_slot(vehicle, setup_idx, slot_idx, device_cd, outcome, ledger, callback=None):
    """Empties one slot whose device leaves this setup entirely. Reports False
    when the occupant has to stay put.

    A device that merely MOVES to another slot of the same setup is never
    cleared: the layout command in phase 3 repositions it for free, exactly
    like the native swapSlots + confirm flow does."""
    cleared = False
    try:
        item = inventory.device_by_cd(device_cd)
        if item is None:
            outcome.errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=device_cd))
            return

        other_setups = [idx for idx in inventory.setup_indices(vehicle) if idx != setup_idx]
        stays_on_vehicle = any(inventory.vehicle_has_device(vehicle, device_cd, setup_idx=idx)
                               for idx in other_setups)
        if stays_on_vehicle:
            # Still mounted in the other setup, so this is free by definition.
            code, extra = yield rpc.equip_device(vehicle.invID, 0, slot_idx, False, False)
        else:
            if not inventory.is_free_to_demount(item):
                outcome.note_skipped(item.userName, t('reasonPaidDemount'), also_missing=False)
                return
            code, extra = yield rpc.equip_device(vehicle.invID, 0, slot_idx,
                                                 True, not item.isRemovable)
        if not rpc.is_success(code):
            outcome.errors.append(t('errDemountFailed', name=item.userName,
                                    code=code, ext=extra))
            return

        if not stays_on_vehicle:
            ledger.freed_one(device_cd)
        yield rpc.pause(rpc.OP_PAUSE)
        cleared = True
    except Exception:
        LOG.exc('_clear_slot failed')
    finally:
        if callback is not None:
            callback(cleared)


@adisp_async
@adisp_process
def _borrow_from_donor(veh_inv_id, device_cd, item, options, outcome, ledger, callback=None):
    """Frees the device from another vehicle so it lands in the depot. Reports
    whether it is now available. Only ever demounts when that is free."""
    obtained = False
    try:
        if not inventory.is_free_to_demount(item):
            outcome.note_skipped(item.userName, t('reasonDonorPaidDemount'))
            return

        donor = inventory.find_donor_vehicle(device_cd, veh_inv_id,
                                             options.excluded_donor_inv_ids)
        if donor is None:
            outcome.note_skipped(item.userName, t('reasonNoDonor'))
            return

        donor_setup_idx, donor_slot_idx = inventory.locate_device_on_vehicle(donor, device_cd)
        if donor_slot_idx is None:
            outcome.note_skipped(item.userName,
                                 t('reasonNotFoundOnDonor', donor=donor.userName))
            return

        donor_original_idx = inventory.active_setup_index(donor)
        must_switch = donor_setup_idx != donor_original_idx
        if must_switch:
            code = yield rpc.change_setup_index(donor.invID, donor_setup_idx)
            if not rpc.is_success(code):
                outcome.skipped.append(
                    (item.userName, t('reasonDonorSetupSwitchFailed', donor=donor.userName)))
                return
            yield rpc.pause(rpc.OP_PAUSE)

        LOG.info('demounting %s from %s (setup %d slot %d)'
                 % (item.name, donor.userName, donor_setup_idx, donor_slot_idx))
        code, extra = yield rpc.equip_device(donor.invID, 0, donor_slot_idx,
                                             True, not item.isRemovable)
        obtained = rpc.is_success(code)
        if obtained:
            outcome.donated.append((item.userName, donor.userName))
            ledger.freed_one(device_cd)
        else:
            outcome.errors.append(t('errDonorDemountFailed', name=item.userName,
                                    donor=donor.userName, code=code, ext=extra))
        yield rpc.pause(rpc.OP_PAUSE)

        if must_switch:
            # Nothing below reads donor state, so no pause is needed.
            code = yield rpc.change_setup_index(donor.invID, donor_original_idx)
            if not rpc.is_success(code):
                LOG.warning('could not switch %s back to setup %d'
                            % (donor.userName, donor_original_idx))
    except Exception:
        LOG.exc('_borrow_from_donor failed')
    finally:
        if callback is not None:
            callback(obtained)


@adisp_async
@adisp_process
def _install_layout(vehicle, setup_idx, final, capacity, outcome, ledger, callback=None):
    """Phase 3: apply the whole prepared layout in one server command."""
    try:
        current = inventory.setup_device_cds(vehicle, setup_idx)
        _drop_unaffordable(vehicle, current, final, capacity, outcome, ledger)
        if current == final:
            return

        changes = sum(1 for i in range(capacity) if final[i] and final[i] != current[i])
        code, error = yield rpc.apply_setup_layout(vehicle.invID, final)
        if rpc.is_success(code):
            outcome.installed += changes
        else:
            outcome.errors.append(t('errSetupApplyFailed', setup=setup_idx + 1,
                                    code=code, err=error))
        yield rpc.pause(rpc.OP_PAUSE)
    except Exception:
        LOG.exc('_install_layout failed')
    finally:
        if callback is not None:
            callback(None)


def _drop_unaffordable(vehicle, current, final, capacity, outcome, ledger):
    """The money guarantee: drop every NEW device from the layout that is not
    verifiably in the depot or already on this vehicle, because the layout
    command would otherwise buy it. Adjusts `final` in place."""
    for slot_idx in range(capacity):
        device_cd = final[slot_idx]
        if not device_cd or device_cd == current[slot_idx]:
            continue
        item = inventory.device_by_cd(device_cd)
        already_here = inventory.vehicle_has_device(vehicle, device_cd)
        if item is None or (ledger.count(device_cd) <= 0 and not already_here):
            outcome.note_skipped(item.userName if item is not None else str(device_cd),
                                 t('reasonNotAvailable'))
            final[slot_idx] = current[slot_idx]
        elif not already_here:
            ledger.took_one(device_cd)


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
            LOG.warning('could not restore active setup %s on %s'
                        % (original_setup_idx, veh_inv_id))
    except Exception:
        LOG.exc('_restore_active_setup failed')
    finally:
        if callback is not None:
            callback(None)


def _summary_lines(outcome):
    lines = []
    if outcome.installed:
        lines.append(t('summaryInstalled', count=outcome.installed))
    for special_name, standard_name in outcome.downgraded:
        lines.append(t('summaryDowngrade', special=special_name, standard=standard_name))
    for device_name, donor_name in outcome.donated:
        lines.append(t('summaryDonated', name=device_name, donor=donor_name))
    for device_name, reason in outcome.skipped:
        lines.append(t('summarySkipped', name=device_name, reason=reason))
    for error in outcome.errors:
        lines.append(t('summaryError', err=error))
    return lines


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

@adisp_process
def apply_saved_sets(veh_inv_id, trigger='selection'):
    """Single-vehicle run, used by the selection trigger. `trigger` only names
    what set the run off, for the metrics row."""
    global _busy
    if _busy:
        return
    _busy = True
    _notify_refresh()
    inventory.reset_donor_search_stats()
    metrics.start_run('single', trigger)
    outcome = None
    try:
        outcome = yield apply_to_vehicle(veh_inv_id, RunOptions())
    except Exception:
        LOG.exc('apply_saved_sets failed')
    finally:
        inventory.log_donor_search_stats('apply_saved_sets(%s)' % veh_inv_id)
        metrics.end_run(**_single_run_totals(outcome))
        _busy = False
        _notify_refresh()


def _single_run_totals(outcome):
    """The metrics row for a one-vehicle run, read off the RunOutcome that run
    already produced. A vehicle counts as touched when it has something to
    report - which is exactly when the run made server calls for it."""
    if outcome is None:
        return {'vehicles_planned': 1, 'vehicles_touched': 0}
    return {
        'vehicles_planned': 1,
        'vehicles_touched': 1 if outcome.has_anything_to_report() else 0,
        'installed': outcome.installed,
        'donor_demounts': len(outcome.donated),
        'downgrades': len(outcome.downgraded),
        'skipped': len(outcome.skipped),
        'errors': len(outcome.errors),
        'interrupted': outcome.interrupted,
    }


def on_vehicle_changed():
    """Hooked to g_currentVehicle.onChanged: applies the saved sets whenever
    the selection moves to a different vehicle."""
    global _last_inv_id
    try:
        vehicle = g_currentVehicle.item
        if vehicle is None or vehicle.invID == _last_inv_id:
            return
        _last_inv_id = vehicle.invID
        _notify_refresh()
        if not config.is_auto_enabled() or not config.has_saved_sets(vehicle.invID):
            return
        # Let the selection settle first, then start - unless the player
        # switched vehicles again in the meantime.
        veh_inv_id = vehicle.invID
        BigWorld.callback(0.05, lambda: _start_if_still_selected(veh_inv_id))
    except Exception:
        LOG.exc('on_vehicle_changed failed')


def _start_if_still_selected(veh_inv_id):
    try:
        if _selection_moved_away(veh_inv_id):
            return
        if _busy:
            LOG.warning('apply already running, skipping trigger for %s' % veh_inv_id)
            return
        apply_saved_sets(veh_inv_id)
    except Exception:
        LOG.exc('_start_if_still_selected failed')


@adisp_process
def equip_primary_vehicles():
    """Popover button: equip every filtered Primary vehicle with its saved
    sets, special devices falling back to their standard variant. Posts one
    summary, plus a second message listing shortages."""
    global _busy
    if _busy:
        messages.push_warning(t('alreadyRunning'))
        return

    # Mode loaners drop out here rather than inside the run: apply_to_vehicle
    # would refuse them anyway, but counting them as "processed" in the summary
    # would claim work that never happened.
    targets = [vehicle for vehicle in inventory.filtered_primary_vehicles()
               if not inventory.is_mode_only_vehicle(vehicle)]
    if not targets:
        messages.push_warning(t('batchNoTargets'))
        return
    with_sets = [v for v in targets if config.has_saved_sets(v.invID)]
    without_sets = len(targets) - len(with_sets)
    if not with_sets:
        messages.push_warning(t('batchNoSavedSets', count=len(targets)))
        return

    # Vehicles in this batch must never donate to each other: without this,
    # vehicle B would happily demount the device vehicle A just received,
    # ping-ponging equipment instead of ending with every Primary equipped.
    # Only vehicles OUTSIDE the batch stay eligible as donors.
    options = RunOptions(watch_selection=False, force_downgrade=True,
                         show_veil=False, push_summary=False,
                         excluded_donor_inv_ids=set(v.invID for v in with_sets))

    _busy = True
    _notify_refresh()
    inventory.reset_donor_search_stats()
    metrics.start_run('batch', 'button')
    veil_shown = messages.show_waiting()
    totals = _BatchTotals()
    try:
        LOG.info('equip_primary_vehicles: %d target(s): %s'
                 % (len(with_sets), [v.userName for v in with_sets]))
        for vehicle in with_sets:
            outcome = yield apply_to_vehicle(vehicle.invID, options)
            totals.add(vehicle, outcome)

        messages.push_lines(totals.summary_lines(without_sets),
                            warning=bool(totals.errors))
        if totals.missing_counts:
            messages.push_error(u'<br/>'.join(totals.missing_lines()))
        _disable_auto_install_after_batch()
        LOG.info('equip_primary_vehicles: done - processed=%d installed=%d missing=%s'
                 % (totals.processed, totals.installed, totals.missing_counts))
    except Exception:
        LOG.exc('equip_primary_vehicles failed')
    finally:
        inventory.log_donor_search_stats(
            'equip_primary_vehicles(%d vehicle(s))' % totals.processed)
        metrics.end_run(vehicles_planned=len(with_sets), **totals.metrics_row())
        if veil_shown:
            messages.hide_waiting()
        _busy = False
        _notify_refresh()


class _BatchTotals(object):
    """Running totals across all vehicles of one batch run."""

    def __init__(self):
        self.processed = 0
        self.installed = 0
        self.donated = 0
        self.downgraded = []
        self.errors = []
        self.missing_counts = {}    # device name -> number of vehicles missing it
        # Only for the metrics row - the player-facing summary does not use them.
        self.touched = 0
        self.skipped = 0
        self.interrupted = False

    def add(self, vehicle, outcome):
        self.processed += 1
        self.installed += outcome.installed
        self.donated += len(outcome.donated)
        self.skipped += len(outcome.skipped)
        if outcome.has_anything_to_report():
            self.touched += 1
        if outcome.interrupted:
            self.interrupted = True
        for note in outcome.downgraded:
            if note not in self.downgraded:
                self.downgraded.append(note)
        for error in outcome.errors:
            self.errors.append(t('batchVehicleError', veh=vehicle.userName, err=error))
        for name in outcome.missing:
            self.missing_counts[name] = self.missing_counts.get(name, 0) + 1

    def summary_lines(self, without_sets):
        lines = [t('batchSummary', processed=self.processed, installed=self.installed)]
        if self.donated:
            lines.append(t('batchDonated', count=self.donated))
        for special_name, standard_name in self.downgraded:
            lines.append(t('summaryDowngrade', special=special_name, standard=standard_name))
        if without_sets:
            lines.append(t('batchSkippedNoSets', count=without_sets))
        for error in self.errors[:_MAX_SUMMARY_ERRORS]:
            lines.append(t('summaryError', err=error))
        if len(self.errors) > _MAX_SUMMARY_ERRORS:
            lines.append(t('batchMoreErrors', count=len(self.errors) - _MAX_SUMMARY_ERRORS))
        return lines

    def metrics_row(self):
        """What end_run() wants, straight out of the sums this class already
        keeps for the summary message."""
        return {
            'vehicles_touched': self.touched,
            'installed': self.installed,
            'donor_demounts': self.donated,
            'downgrades': len(self.downgraded),
            'skipped': self.skipped,
            'errors': len(self.errors),
            'interrupted': self.interrupted,
        }

    def missing_lines(self):
        lines = [t('batchMissingHeader'), t('batchMissingListHeader')]
        for name in sorted(self.missing_counts):
            lines.append(t('batchMissingLine', count=self.missing_counts[name], name=name))
        return lines


def _disable_auto_install_after_batch():
    """Auto-install would otherwise re-shuffle equipment the moment the player
    browses through their OTHER vehicles right after a batch: each selection
    re-triggers a run, which can pull a device straight back off a Primary
    vehicle. Turning it off protects the freshly equipped fleet - and is only
    announced when it actually changed something."""
    if not config.is_auto_enabled():
        return
    config.set_auto_enabled(False)
    messages.push_warning(t('autoDisabledAfterBatch'),
                          priority=NotificationPriorityLevel.HIGH)
