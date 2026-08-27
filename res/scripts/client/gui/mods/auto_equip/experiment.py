# -*- coding: utf-8 -*-
"""A one-shot, opt-in probe answering ONE question:

    does apply_setup_layout (CMD_EQUIP_OPT_DEVS_SEQUENCE) with a 0 in a slot
    take that device OFF and put it in the depot?

WHY IT MATTERS: per-slot demounting is the mod's most expensive habit. In the
optimization1 run, 171 of 250 state-changing calls were single demounts. If the
whole-layout command already removes what it finds missing from the list, a
vehicle could be cleared and re-equipped in ONE call instead of one per slot -
and the setup switches that go with them would largely disappear too. If it does
not, the idea is dead and nothing more needs to be built on it.

The decompiled client does not answer this. The wire command is
`[shopRev, vehInvID, len(devices), *devices]` - no finance flag, no demount-kit
flag, nothing that says how a removal should be paid for. The native caller,
OptDevicesInstaller, only ever validates the BUY side (MoneyValidator over the
items that are not in the inventory yet). What the server does with a slot that
went from "device" to 0 is simply not visible from here. Hence a live probe.

HOW IT STAYS SAFE:

  * It only ever touches a device that inventory.is_free_to_demount() approves -
    the same gate the whole mod uses. With no finance flag on the wire there is
    no way to tell the server "only if free", so the caller has to pick a case
    where free is the only possible answer.
  * It only touches a device that is NOT in the vehicle's other setup, so the
    device really has to leave the vehicle for the question to be answered.
  * Every other slot is sent back its CURRENT content, so there is nothing for
    the server to buy - which is the one thing this command is known to do.
  * Credits and gold are read before and after and compared. A charge cannot be
    prevented by us here, but it can be caught and reported.
  * The restore step re-applies the original layout ONLY when the device is
    verifiably in the depot. Restoring a device that is not there would ask the
    buy-and-install command to buy it.

It runs at most once: the config flag that arms it is cleared before the first
server call, so a failure part-way through cannot arm a second attempt.

The player-visible strings here are plain English literals rather than i18n
keys, on purpose: this is a developer probe behind a config flag no normal
install ever sets, and it is meant to be deleted once it has answered.
"""

from adisp import adisp_process
from helpers import dependency
from skeletons.gui.shared import IItemsCache

from . import config, inventory, messages, rpc
from .log import LOG

# Long enough for the items cache to have resynced before we judge the result.
# The usual OP_PAUSE is 10ms and exists to smooth over ordering, not to wait for
# a full depot update - and a wrong reading here would answer the question wrong.
_SETTLE = 0.5

_running = False


def _money():
    """(credits, gold). Read straight before and after the probe: the command
    carries no finance flag, so this is the only way to notice a charge."""
    try:
        stats = dependency.instance(IItemsCache).items.stats
        return int(stats.credits), int(stats.gold)
    except Exception:
        LOG.exc('probe: could not read the account balance')
        return None, None


def run_if_pending(veh_inv_id):
    """Starts the probe when it is armed. Reports whether it took over this
    vehicle selection - the caller must then leave the vehicle alone, because an
    install run on top of the probe would fight it."""
    try:
        if _running or not config.is_layout_probe_armed():
            return False
        # Disarmed FIRST: whatever happens below, this must not run twice.
        config.set_layout_probe_armed(False)
        LOG.step('layout demount probe: armed, taking over vehicle %s' % veh_inv_id)
        probe_layout_demount(veh_inv_id)
        return True
    except Exception:
        LOG.exc('probe: could not start')
        return False


def _pick_slot(vehicle, setup_idx):
    """(slot index, item) of a device this probe may safely move, or (None, None).

    Wanted: mounted, free to demount, and not also sitting in the other setup -
    the last one because a device that stays on the vehicle would never reach the
    depot, and then the probe could not tell 'no depot' from 'never left'."""
    others = [idx for idx in inventory.setup_indices(vehicle) if idx != setup_idx]
    for slot_idx, device_cd in enumerate(inventory.setup_device_cds(vehicle, setup_idx)):
        if not device_cd:
            continue
        item = inventory.device_by_cd(device_cd)
        if item is None or not inventory.is_free_to_demount(item):
            continue
        if any(inventory.vehicle_has_device(vehicle, device_cd, setup_idx=idx)
               for idx in others):
            continue
        return slot_idx, item
    return None, None


@adisp_process
def probe_layout_demount(veh_inv_id):
    global _running
    _running = True
    veil_shown = False
    try:
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        if vehicle is None:
            _fail('the vehicle vanished')
            return
        if vehicle.isLocked:
            _fail('%s is locked' % vehicle.userName)
            return
        if inventory.is_mode_only_vehicle(vehicle):
            _fail('%s is a mode-locked loaner' % vehicle.userName)
            return

        setup_idx = inventory.active_setup_index(vehicle)
        before_layout = inventory.setup_device_cds(vehicle, setup_idx)
        slot_idx, item = _pick_slot(vehicle, setup_idx)
        if slot_idx is None:
            # Python 2 forbids `return <value>` inside a generator, and this
            # whole function is one - adisp_process drives it.
            _fail('%s carries nothing this probe may safely move - it needs a '
                  'free-to-demount device that is not also in the other setup'
                  % vehicle.userName)
            return

        stock_before = int(getattr(item, 'inventoryCount', 0))
        credits_before, gold_before = _money()
        probe_layout = list(before_layout)
        probe_layout[slot_idx] = 0

        LOG.step('probe: %s, setup %d, slot %d, device %s (%d), depot stock %d, '
                 'layout %s -> %s'
                 % (vehicle.userName, setup_idx, slot_idx, item.userName,
                    item.intCD, stock_before, before_layout, probe_layout))
        veil_shown = messages.show_waiting()

        code, error = yield rpc.apply_setup_layout(
            vehicle.invID, probe_layout,
            meta={'setup_idx': setup_idx, 'device_cd': item.intCD,
                  'device_name': item.userName})
        if not rpc.is_success(code):
            _fail('the server refused the layout command: code %s, %s'
                  % (code, error))
            return
        yield rpc.pause(_SETTLE)

        # --- what actually happened -------------------------------------
        vehicle = inventory.vehicle_by_inv_id(veh_inv_id)
        fresh = inventory.device_by_cd(item.intCD)
        after_layout = inventory.setup_device_cds(vehicle, setup_idx) if vehicle else []
        stock_after = int(getattr(fresh, 'inventoryCount', 0)) if fresh else 0
        credits_after, gold_after = _money()
        came_off = bool(after_layout) and not after_layout[slot_idx]
        in_depot = stock_after > stock_before

        LOG.step('probe result: layout %s, slot %d %s, depot stock %d -> %d, '
                 'credits %s -> %s, gold %s -> %s'
                 % (after_layout, slot_idx, 'EMPTY' if came_off else 'still filled',
                    stock_before, stock_after, credits_before, credits_after,
                    gold_before, gold_after))

        _report_money(credits_before, credits_after, gold_before, gold_after)

        if came_off and in_depot:
            verdict = ('AutoEquip probe: YES - the layout command demounted %s '
                       'and it reached the depot. One call can replace the '
                       'per-slot demounts.' % item.userName)
        elif came_off:
            verdict = ('AutoEquip probe: PARTLY - %s came off the vehicle but the '
                       'depot count did not rise (%d). Do NOT build on this; check '
                       'the depot by hand before running anything else.'
                       % (item.userName, stock_after))
        else:
            verdict = ('AutoEquip probe: NO - the layout command left %s mounted. '
                       'Per-slot demounts stay necessary.' % item.userName)
        LOG.step(verdict)
        messages.push_info(verdict)

        # --- put it back ------------------------------------------------
        if not came_off:
            return          # nothing moved, nothing to restore
        if not in_depot:
            # Re-applying the original layout would ask the buy-and-install
            # command for a device it cannot find in the depot. It would buy it.
            LOG.warning('probe: NOT restoring - %s is not in the depot and the '
                        'layout command would purchase it' % item.userName)
            messages.push_error('AutoEquip probe: slot %d on %s was left empty on '
                                'purpose - restoring it could have cost money.'
                                % (slot_idx + 1, vehicle.userName))
            return

        code, error = yield rpc.apply_setup_layout(
            vehicle.invID, before_layout,
            meta={'setup_idx': setup_idx, 'device_cd': item.intCD,
                  'device_name': item.userName})
        yield rpc.pause(_SETTLE)
        restored = inventory.vehicle_by_inv_id(veh_inv_id)
        final_layout = inventory.setup_device_cds(restored, setup_idx) if restored else []
        ok = final_layout == before_layout
        LOG.step('probe restore: code %s, layout %s, %s'
                 % (code, final_layout, 'back as it was' if ok else 'MISMATCH'))
        credits_end, gold_end = _money()
        _report_money(credits_after, credits_end, gold_after, gold_end)
        if not ok:
            messages.push_error('AutoEquip probe: %s was NOT fully restored - '
                                'expected %s, found %s. Please check the vehicle.'
                                % (vehicle.userName, before_layout, final_layout))
    except Exception:
        LOG.exc('probe_layout_demount failed')
        messages.push_error('AutoEquip probe: crashed - see python.log. Check the '
                            'vehicle before doing anything else.')
    finally:
        _running = False
        if veil_shown:
            messages.hide_waiting()


def _report_money(credits_before, credits_after, gold_before, gold_after):
    """Says so loudly if the account got poorer. This cannot undo a charge - the
    command has no finance flag to hold it back - but an unnoticed one would be
    the worst possible outcome of an experiment."""
    if None in (credits_before, credits_after, gold_before, gold_after):
        return
    spent_credits = credits_before - credits_after
    spent_gold = gold_before - gold_after
    if spent_credits <= 0 and spent_gold <= 0:
        return
    LOG.error('probe: THE ACCOUNT WAS CHARGED - %d credits, %d gold'
              % (spent_credits, spent_gold))
    messages.push_error('AutoEquip probe: the server charged %d credits and %d '
                        'gold. Please report this - the probe was supposed to be '
                        'free.' % (spent_credits, spent_gold))


def _fail(reason):
    LOG.warning('probe: not run - %s' % reason)
    messages.push_warning('AutoEquip probe: not run - %s' % reason)
