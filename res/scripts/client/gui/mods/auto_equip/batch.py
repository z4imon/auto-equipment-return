# -*- coding: utf-8 -*-
"""PARKED - measured, slower, and NOT wired into the mod. Kept as evidence.

Nothing imports this module. It was tried on a live account on 2026-08-27 and
lost to the per-slot path it was meant to replace:

                        per-slot (optimization1)   batched (this)
    calls per device            0.894                  0.719
    ms per device               382                    586      <- 53% slower
    time spent in backoff         9.5%                  38.4%
    devices lost                  0                      4

The batching worked exactly as predicted - a fifth fewer server calls. It lost
anyway, for two reasons, and both are worth remembering:

  1. CMD_EASY_TANK_EQUIP_APPLY is rate-limited far harder than the per-slot
     command. Two thirds of its calls came back RES_COOLDOWN, a third needed
     four retries; attempts that went through had ~0.6s of clear air behind
     them. Per-slot demounts were never throttled once in two sessions and fire
     back-to-back at ~290ms. Batching therefore only pays from roughly 3.5
     devices per call upward - and a donor gives 1.34 on average.
  2. Planning a whole setup up front reads a stale items cache. After the first
     setup was installed, planning the second one could not see the devices that
     had just been fitted and borrowed them a second time from a donor that no
     longer had them. That is where the four lost devices came from. The serial
     path never had the problem because it re-reads before every single slot.

If this is ever revisited: fix (2) first by re-reading between setups, and pace
the calls instead of retrying into the throttle - but (1) is the wall, and it
does not move.

--- what it did ---------------------------------------------------------------

Bringing one setup to its saved layout with as few server calls as possible.

The serial path in apply.py spends one call per device leaving a vehicle plus
one for the layout. This one uses CMD_EASY_TANK_EQUIP_APPLY, the only command
that takes SEVERAL optional devices off a vehicle at once - and applies the new
layout in the same call. A vehicle that needed four calls can need one.

    donor A:  take these three devices off        1 call
    donor B:  take this one off                   1 call
    vehicle:  take these off AND install that     1 call

WHY IT IS A SEPARATE MODULE: the serial path stays exactly as it was and remains
the fallback. Nothing here has been seen working on a live server - the command
is gated by a server-side `enabled` flag that defaults to False, and the client
additionally hides its own button below a configurable vehicle tier. Whether the
SERVER enforces that tier is not visible from the client source, so this tries
the command and falls back when refused, rather than refusing on the server's
behalf. First refusal switches the whole session back to the serial path.

MONEY GUARANTEE, unchanged in substance:

  * The demount-kit list is not a parameter of rpc.easy_tank_equip - it is
    hardcoded empty. A kit cannot be spent by this path at all.
  * Every device named for demounting passes inventory.is_free_to_demount(),
    the same gate the serial path applies. The command has no finance flag, so
    that check is the only thing between the player and a charge.
  * A device is named in the layout only when it is verifiably obtainable:
    already on the vehicle, in the depot, or secured from a donor in this run.
    The layout half of the command BUYS what it cannot find, exactly like
    apply_setup_layout does.

WHAT IT REFUSES TO HANDLE: a device that has to leave THIS setup while staying
in the other one. The serial path frees such a slot with allSetups=False, which
this command has no equivalent for - it takes devices off the vehicle. Planning
returns None for that setup and the serial path takes it.
"""

from adisp import adisp_async, adisp_process

from . import inventory, rpc
from .i18n import t
from .log import LOG

# Set when the server refuses the batched command, and then left set for the
# rest of the session: a refusal is a property of the account or the server
# configuration, not of one vehicle, so retrying it per setup would only pay for
# the same rejection again and again.
_refused = False


def is_refused():
    return _refused


def note_refused(code, error):
    global _refused
    if _refused:
        return
    _refused = True
    LOG.warning('batched equip refused by the server (code %s, %s) - falling '
                'back to the per-slot path for the rest of this session'
                % (code, error))


def reset():
    """Lets a later session try again. Only for tests and teardown."""
    global _refused
    _refused = False


# ---------------------------------------------------------------------------
# Planning - no server calls, no side effects beyond the outcome report
# ---------------------------------------------------------------------------

class SetupPlan(object):
    """Everything one setup needs, decided before anything is sent.

    `final` is the layout that will actually be applied; slots that could not be
    served fall back to what is already there, exactly as the serial path does.
    """

    def __init__(self, final):
        self.final = final
        self.leaving = []       # device intCDs to take off the vehicle
        self.borrows = {}       # donor invID -> [device intCD]
        self.donors = {}        # donor invID -> user name, for the report


def plan_setup(vehicle, setup_idx, wanted, options, outcome):
    """A SetupPlan, or None when this setup has to go the serial way.

    Nothing here talks to the server, so a None costs only the reads it made.

    It also deliberately does NOT touch apply.py's DepotLedger. Planning happens
    before anything moves, and a plan that ends up handed back to the serial path
    must leave no trace - a ledger credited for a demount that never went out
    would let the serial run name a device the depot does not have, and the
    layout command buys what it cannot find. Depot stock is counted locally
    instead, seeded per device from the items cache.
    """
    try:
        capacity = inventory.slot_capacity(vehicle)
        current = inventory.setup_device_cds(vehicle, setup_idx)
        others = [idx for idx in inventory.setup_indices(vehicle) if idx != setup_idx]
        plan = SetupPlan(list(wanted))
        depot = {}

        for slot_idx in range(capacity):
            current_cd = current[slot_idx]
            wanted_cd = plan.final[slot_idx]
            if current_cd == wanted_cd:
                continue

            if current_cd and current_cd not in wanted:
                if not _plan_removal(vehicle, slot_idx, current_cd,
                                     others, plan, outcome, depot):
                    if plan.final is None:
                        return None     # the serial path has to take this setup
                    continue

            if not wanted_cd:
                continue
            if not _plan_source(vehicle, slot_idx, wanted_cd, options, plan,
                                outcome, depot):
                continue
        return plan
    except Exception:
        LOG.exc('plan_setup failed')
        return None


def _plan_removal(vehicle, slot_idx, device_cd, others, plan, outcome, depot):
    """Whether the occupant of this slot can be taken off by the batched command.
    Sets plan.final to None when the whole setup has to go the serial way."""
    item = inventory.device_by_cd(device_cd)
    if item is None:
        outcome.errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=device_cd))
        plan.final[slot_idx] = device_cd
        return False

    if any(inventory.vehicle_has_device(vehicle, device_cd, setup_idx=idx)
           for idx in others):
        # It only has to leave THIS setup. The batched command takes devices off
        # the vehicle; freeing one setup alone is what allSetups=False is for,
        # and that lives on the per-slot call.
        LOG.info('batched: %s keeps %s in its other setup - serial path'
                 % (vehicle.userName, item.userName))
        plan.final = None
        return False

    if not inventory.is_free_to_demount(item):
        outcome.note_skipped(item.userName, t('reasonPaidDemount'), also_missing=False)
        plan.final[slot_idx] = device_cd
        return False

    plan.leaving.append(device_cd)
    depot[device_cd] = _depot_count(item, depot) + 1
    return True


def _depot_count(item, depot):
    """How many of this device the plan believes are in the depot, seeded once
    from the items cache and then kept up to date by the plan itself."""
    if item.intCD not in depot:
        depot[item.intCD] = int(getattr(item, 'inventoryCount', 0))
    return depot[item.intCD]


def _plan_source(vehicle, slot_idx, device_cd, options, plan, outcome, depot):
    """Makes sure the wanted device will be available, or empties the slot.

    Availability is counted, not just flagged: a saved set that names the same
    device for two slots needs two copies, and one in the depot serves only one
    of them. The game itself does not allow duplicates on a vehicle, but an
    imported or hand-edited set can carry them, and the layout half of the
    command BUYS whatever it is told to install and cannot find."""
    item = inventory.device_by_cd(device_cd)
    if item is None:
        outcome.errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=device_cd))
        plan.final[slot_idx] = 0
        return False
    if inventory.vehicle_has_device(vehicle, device_cd):
        return True
    if _depot_count(item, depot) > 0:
        depot[device_cd] -= 1
        return True

    if not inventory.is_free_to_demount(item):
        outcome.note_skipped(item.userName, t('reasonDonorPaidDemount'))
        plan.final[slot_idx] = 0
        return False
    donor = inventory.find_donor_vehicle(device_cd, vehicle.invID,
                                         options.excluded_donor_inv_ids)
    if donor is None:
        outcome.note_skipped(item.userName, t('reasonNoDonor'))
        plan.final[slot_idx] = 0
        return False

    borrowed = plan.borrows.setdefault(donor.invID, [])
    if device_cd in borrowed:
        # This donor was already asked for this device and carries only the one.
        outcome.note_skipped(item.userName, t('reasonNoDonor'))
        plan.final[slot_idx] = 0
        return False
    borrowed.append(device_cd)
    plan.donors[donor.invID] = donor.userName
    return True


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@adisp_async
@adisp_process
def run_plan(veh_inv_id, setup_idx, plan, outcome, callback=None):
    """Executes a SetupPlan. Reports True when the batched path carried the
    setup through, False when the caller must run the serial path instead.

    False is only ever reported before anything changed, or after a refusal that
    left the vehicle as it was - a half-applied setup is reported as an error and
    NOT retried serially, because the serial path would then plan against a state
    this one already moved."""
    ok = False
    try:
        for donor_inv_id in sorted(plan.borrows):
            device_cds = plan.borrows[donor_inv_id]
            got = yield _borrow_batch(donor_inv_id, device_cds,
                                      plan.donors.get(donor_inv_id), outcome)
            if not got:
                # Nothing was taken from this donor. Drop what it owed us from
                # the layout so the install cannot ask the server to buy it.
                for device_cd in device_cds:
                    _drop_from_layout(plan, device_cd)
                if is_refused():
                    return
        ok = yield _install(veh_inv_id, setup_idx, plan, outcome)
    except Exception:
        LOG.exc('run_plan failed')
    finally:
        if callback is not None:
            callback(ok)


def _drop_from_layout(plan, device_cd):
    for slot_idx, cd in enumerate(plan.final):
        if cd == device_cd:
            plan.final[slot_idx] = 0


@adisp_async
@adisp_process
def _borrow_batch(donor_inv_id, device_cds, donor_name, outcome, callback=None):
    """Takes several devices off ONE donor in a single call. Reports whether
    they are now free.

    The layout sent along is the donor's own, minus what is being taken: the
    command applies a layout as well, and leaving it out would mean guessing what
    the server does with an empty one. Everything named in it is already mounted
    on that donor, so there is nothing for it to buy."""
    got = False
    try:
        donor = inventory.vehicle_by_inv_id(donor_inv_id)
        if donor is None:
            return
        names = []
        for device_cd in device_cds:
            item = inventory.device_by_cd(device_cd)
            if item is None or not inventory.is_free_to_demount(item):
                # Planning already checked this; re-checked here because the
                # cache may have resynced since, and a paid demount cannot be
                # taken back.
                LOG.warning('batched: %s is no longer free to demount - dropping '
                            'the whole borrow from %s' % (device_cd, donor_inv_id))
                return
            names.append(item.userName)

        setup_idx = inventory.active_setup_index(donor)
        remaining = [0 if cd in device_cds else cd
                     for cd in inventory.setup_device_cds(donor, setup_idx)]
        LOG.info('batched: taking %s off %s in one call'
                 % (names, donor_name or donor_inv_id))
        code, error = yield rpc.easy_tank_equip(
            donor_inv_id, device_cds, remaining,
            meta={'setup_idx': setup_idx, 'device_name': u', '.join(names),
                  'veh_name': donor_name})
        if not rpc.is_success(code):
            note_refused(code, error)
            outcome.errors.append(t('errDonorDemountFailed',
                                    name=u', '.join(names),
                                    donor=donor_name or donor_inv_id,
                                    code=code, ext=error))
            return
        for name in names:
            outcome.donated.append((name, donor_name or unicode(donor_inv_id)))
        got = True
        yield rpc.pause(rpc.OP_PAUSE)
    except Exception:
        LOG.exc('_borrow_batch failed')
    finally:
        if callback is not None:
            callback(got)


@adisp_async
@adisp_process
def _install(veh_inv_id, setup_idx, plan, outcome, callback=None):
    """Takes the leaving devices off the vehicle and applies the layout, in one
    call. Reports whether the setup is done."""
    ok = False
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            return
        current = inventory.setup_device_cds(vehicle, setup_idx)
        _drop_unobtainable(vehicle, current, plan, outcome)
        leaving = [cd for cd in plan.leaving if cd in current]
        if current == plan.final and not leaving:
            ok = True
            return                          # nothing left to do
        changes = sum(1 for i, cd in enumerate(plan.final)
                      if cd and cd != current[i])

        code, error = yield rpc.easy_tank_equip(
            vehicle.invID, leaving, plan.final,
            meta={'setup_idx': setup_idx, 'veh_name': vehicle.userName})
        if not rpc.is_success(code):
            note_refused(code, error)
            # Nothing was applied, so the serial path can still take this setup
            # from a state it fully understands.
            return
        outcome.installed += changes
        ok = True
        yield rpc.pause(rpc.OP_PAUSE)
    except Exception:
        LOG.exc('_install failed')
    finally:
        if callback is not None:
            callback(ok)


def _drop_unobtainable(vehicle, current, plan, outcome):
    """The money guarantee for the layout half: a device that is neither on this
    vehicle nor in the depot would be BOUGHT. Adjusts plan.final in place."""
    for slot_idx, device_cd in enumerate(plan.final):
        if not device_cd or device_cd == current[slot_idx]:
            continue
        item = inventory.device_by_cd(device_cd)
        if item is None:
            plan.final[slot_idx] = current[slot_idx]
            continue
        if inventory.vehicle_has_device(vehicle, device_cd):
            continue
        if int(getattr(item, 'inventoryCount', 0)) > 0:
            continue
        outcome.note_skipped(item.userName, t('reasonNotAvailable'))
        plan.final[slot_idx] = current[slot_idx]
