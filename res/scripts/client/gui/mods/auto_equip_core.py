# -*- coding: utf-8 -*-
"""Equipment engine: snapshots the two optional-device setups of a vehicle and
re-installs them later, pulling missing devices from the depot or — free of
charge via WoT Plus — from other vehicles.

Money guarantee: no operation in this module ever spends gold or a demount kit.
Every demount that would cost anything is skipped and reported instead —
_free_demount_ok gates all of them. The raw inventory RPCs used here show no
confirm dialogs, so a skipped check would silently charge."""

import BigWorld

from auto_equip_log import LOG
from auto_equip_i18n import t

from adisp import adisp_async, adisp_process
from helpers import dependency
from skeletons.gui.shared import IItemsCache
from skeletons.gui.game_control import IWotPlusController
from post_progression_common import TankSetupGroupsId

_OPT_GROUP = TankSetupGroupsId.OPTIONAL_DEVICES_AND_BOOSTERS

# Pause between server operations so the items cache settles before we read it
# again (the equip callbacks can fire before the resync is fully applied).
# Stale reads here are safe-direction only: the server state is authoritative,
# worst case is a skipped install with a message — never a purchase.
_OP_PAUSE = 0.01

_g_busy = False
_g_abort = False
_g_last_cd = None          # last vehicle intCD we reacted to (dedupes resync onChanged storms)
_g_refresh_cb = None       # gameface callback: refresh popover data after state changes


def set_refresh_cb(cb):
    global _g_refresh_cb
    _g_refresh_cb = cb


def _notify_refresh():
    if _g_refresh_cb is not None:
        try:
            _g_refresh_cb()
        except Exception:
            LOG.exc('_notify_refresh failed')


def is_busy():
    return _g_busy


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _items_cache():
    return dependency.instance(IItemsCache)


def _wot_plus():
    return dependency.instance(IWotPlusController)


def has_wot_plus():
    try:
        return _wot_plus().hasSubscription()
    except Exception:
        LOG.exc('has_wot_plus check failed')
        return False


def _fresh_vehicle(veh_cd):
    """Always re-fetch the gui item — instances are recreated on cache sync."""
    try:
        return _items_cache().items.getItemByCD(veh_cd)
    except Exception:
        LOG.exc('_fresh_vehicle(%s) failed' % veh_cd)
        return None


def _fresh_item(int_cd):
    try:
        return _items_cache().items.getItemByCD(int_cd)
    except Exception:
        return None


def _free_demount_ok(item):
    """True if demounting this device is guaranteed to cost nothing.
    Removable devices (binocs & co.) always demount for free; everything else
    only with the WoT Plus free-demount rules (regular/trophy/experimental lvl 1,
    NOT improved, NOT experimental lvl 2/3)."""
    try:
        if item.isRemovable:
            return True
        return _wot_plus().isFreeToDemount(item)
    except Exception:
        LOG.exc('_free_demount_ok failed for %s' % getattr(item, 'name', '?'))
        return False


def _setup_indices(vehicle):
    try:
        return sorted(vehicle.optDevices.setupLayouts.setups.keys())
    except Exception:
        LOG.exc('_setup_indices failed')
        return [0]


def _capacity(vehicle):
    return vehicle.optDevices.installed.getCapacity()


def _setup_cds(vehicle, setup_idx):
    """Per-slot intCD list (0 = empty) of one setup, trimmed to slot capacity."""
    cds = vehicle.optDevices.setupLayouts.getIntCDs(setupIdx=setup_idx)
    cap = _capacity(vehicle)
    cds = list(cds)[:cap]
    while len(cds) < cap:
        cds.append(0)
    return [int(cd) if cd else 0 for cd in cds]


def has_second_setup(vehicle):
    return 1 in _setup_indices(vehicle)


def snapshot_sets(vehicle):
    """Current setups as {'set1': [...], 'set2': [...] or None}."""
    set1 = _setup_cds(vehicle, 0)
    set2 = _setup_cds(vehicle, 1) if has_second_setup(vehicle) else None
    return {'set1': set1, 'set2': set2}


_g_overview_logged = False


def log_equipment_overview():
    """Logs every owned optional device: name, intCD, depot count and on how
    many vehicles it is mounted. A device sitting in both setups of one vehicle
    is one physical item, so it counts once per vehicle. Runs once per session."""
    global _g_overview_logged
    if _g_overview_logged:
        return
    try:
        from gui.shared.gui_items import GUI_ITEM_TYPE
        from gui.shared.utils.requesters import REQ_CRITERIA
        mounted = {}
        vehicles = _items_cache().items.getVehicles(REQ_CRITERIA.INVENTORY)
        for veh in vehicles.itervalues():
            cds = set()
            try:
                for setup_idx in _setup_indices(veh):
                    cds.update(cd for cd in _setup_cds(veh, setup_idx) if cd)
            except Exception:
                continue
            for cd in cds:
                mounted[cd] = mounted.get(cd, 0) + 1
        devices = _items_cache().items.getItems(GUI_ITEM_TYPE.OPTIONALDEVICE, REQ_CRITERIA.EMPTY)
        rows = []
        for cd, item in devices.iteritems():
            depot = item.inventoryCount
            on_veh = mounted.get(int(cd), 0)
            if depot <= 0 and on_veh <= 0:
                continue
            rows.append((item.userName, int(cd), depot, on_veh))
        rows.sort()
        _g_overview_logged = True
        LOG.info('equipment overview: %d owned optional devices' % len(rows))
        for name, cd, depot, on_veh in rows:
            LOG.info((u'  %s | cd=%d | Lager=%d | montiert auf %d Fahrzeug(en)'
                      % (name, cd, depot, on_veh)).encode('utf-8'))
    except Exception:
        LOG.exc('log_equipment_overview failed')


def _push_msg(text, warning=False, error=False):
    try:
        from gui import SystemMessages
        if error:
            sm_type = SystemMessages.SM_TYPE.Error
        elif warning:
            sm_type = SystemMessages.SM_TYPE.Warning
        else:
            sm_type = SystemMessages.SM_TYPE.Information
        SystemMessages.pushMessage(text, type=sm_type)
    except Exception:
        LOG.exc('_push_msg failed')


_WAITING_KEY = 'installEquipment'   # native veil text: "Mounting equipment..."


def _waiting_show():
    """Blocking gray hangar veil — the same one the client shows while
    mounting equipment. Returns True when it was actually shown so the caller
    knows a matching _waiting_hide is required."""
    try:
        from gui.Scaleform.Waiting import Waiting
        Waiting.show(_WAITING_KEY)
        return True
    except Exception:
        LOG.exc('_waiting_show failed')
        return False


def _waiting_hide():
    try:
        from gui.Scaleform.Waiting import Waiting
        Waiting.hide(_WAITING_KEY)
    except Exception:
        LOG.exc('_waiting_hide failed')


def _norm_layout(raw, cap):
    """Saved cd list → per-slot layout of exactly `cap` entries (0 = empty)."""
    cds = [int(cd) if cd else 0 for cd in list(raw)[:cap]]
    while len(cds) < cap:
        cds.append(0)
    return cds


@adisp_async
def _pause(seconds, callback=None):
    BigWorld.callback(seconds, lambda: callback(None))


# --------------------------------------------------------------------------
# Direct inventory RPCs.
#
# We deliberately do NOT use the GUI processor classes (OptDeviceInstaller,
# ChangeVehicleSetupEquipments): their constructors eagerly resolve R.strings
# for confirm dialogs and crash on the live client where those strings changed
# (AttributeError: 'dialogs' object has no attribute 'confirmationNotRemovable').
# These are the same server calls the processors make in _request(); the server
# validates everything and returns a negative code on failure.
# --------------------------------------------------------------------------

def _op_success(code):
    return code >= 0


_RES_COOLDOWN = -5      # AccountCommands.RES_COOLDOWN
_COOLDOWN_RETRIES = 4


def _retry_on_cooldown(fire, callback, attempt=0):
    """Runs an RPC and re-issues it when the server answers RES_COOLDOWN.
    fire(done) must issue the RPC and report via done(code, result); the pause
    grows per attempt (0.1/0.2/0.3/0.4s), after that the cooldown result is
    passed through."""
    def done(code, result):
        if code == _RES_COOLDOWN and attempt < _COOLDOWN_RETRIES:
            delay = 0.1 * (attempt + 1)
            LOG.info('server cooldown, retrying in %.1fs (attempt %d)' % (delay, attempt + 1))
            BigWorld.callback(delay, lambda: _retry_on_cooldown(fire, callback, attempt + 1))
        else:
            callback(result)
    fire(done)


@adisp_async
def _change_setup_index(veh_inv_id, setup_idx, callback=None):
    def fire(done):
        try:
            BigWorld.player().inventory.changeVehicleSetupGroup(
                veh_inv_id, _OPT_GROUP, setup_idx, lambda code: done(code, code))
        except Exception:
            LOG.exc('_change_setup_index RPC failed')
            done(-1, -1)
    _retry_on_cooldown(fire, callback)


@adisp_async
def _equip_opt_device(veh_inv_id, item_cd, slot_idx, all_setups, finance_operation, callback=None):
    """item_cd = device intCD to install, or 0 to demount the slot.
    Never passes useDemountKit — demount kits must not be spent."""
    def fire(done):
        def _cb(code, ext=None):
            done(code, (code, ext))
        try:
            BigWorld.player().inventory.equipOptionalDevice(
                veh_inv_id, item_cd, slot_idx, all_setups, finance_operation, _cb, False)
        except Exception:
            LOG.exc('_equip_opt_device RPC failed')
            done(-1, (-1, None))
    _retry_on_cooldown(fire, callback)


@adisp_async
def _equip_opt_devs_sequence(veh_inv_id, device_cds, callback=None):
    """Apply the WHOLE opt-device layout of the ACTIVE setup in one command
    (CMD_EQUIP_OPT_DEVS_SEQUENCE) — the native tank-setup confirm flow. This is
    the only server path that accepts a device mounted in the OTHER setup of
    the same vehicle; per-slot equipOptionalDevice rejects that with
    RES_WRONG_ARGS (-2). CAUTION: this is the buy-and-install command — callers
    must ensure every listed device is in the depot or on the vehicle, or the
    server would buy it."""
    cds = [int(cd) for cd in device_cds]
    def fire(done):
        def _cb(code, err_str='', ext=None):
            done(code, (code, err_str))
        try:
            BigWorld.player().inventory.equipOptDevsSequence(veh_inv_id, cds, _cb)
        except Exception:
            LOG.exc('_equip_opt_devs_sequence RPC failed')
            done(-1, (-1, 'rpc failed'))
    _retry_on_cooldown(fire, callback)


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------

def save_sets(which):
    """Snapshot the current vehicle's setups into the config.
    which: 1 = set 1, 2 = set 2, 3 = both. Returns a user-facing status text."""
    import mod_auto_equip
    from CurrentVehicle import g_currentVehicle
    vehicle = g_currentVehicle.item
    if vehicle is None:
        return t('noVehicleSelected')
    snap = snapshot_sets(vehicle)
    set1 = snap['set1'] if which in (1, 3) else None
    set2 = snap['set2'] if which in (2, 3) else None
    if which == 2 and snap['set2'] is None:
        return t('noSecondSetup')
    mod_auto_equip.store_sets(vehicle.intCD, set1=set1, set2=set2)
    LOG.info('saved sets for %s (which=%s): set1=%s set2=%s' % (vehicle.userName, which, set1, set2))
    if which == 1:
        return t('set1Saved')
    if which == 2:
        return t('set2Saved')
    return t('bothSetsSaved')


# --------------------------------------------------------------------------
# Vehicle-selection trigger
# --------------------------------------------------------------------------

def on_vehicle_changed():
    """Called from g_currentVehicle.onChanged. Applies the saved sets when the
    selection moved to a different vehicle."""
    global _g_last_cd
    try:
        import mod_auto_equip
        from CurrentVehicle import g_currentVehicle
        item = g_currentVehicle.item
        if item is None:
            return
        if item.intCD == _g_last_cd:
            return
        _g_last_cd = item.intCD
        _notify_refresh()
        if not mod_auto_equip.is_auto_enabled():
            return
        saved = mod_auto_equip.get_sets(item.intCD)
        if not saved or (saved.get('set1') is None and saved.get('set2') is None):
            return
        # Give the selection a moment to settle, then start (unless the user
        # switched again in between).
        veh_cd = item.intCD
        BigWorld.callback(0.05, lambda: _start_if_still_selected(veh_cd))
    except Exception:
        LOG.exc('on_vehicle_changed failed')


def _start_if_still_selected(veh_cd):
    try:
        from CurrentVehicle import g_currentVehicle
        item = g_currentVehicle.item
        if item is None or item.intCD != veh_cd:
            return
        if _g_busy:
            LOG.warning('apply already running, skipping trigger for %s' % veh_cd)
            return
        apply_sets(veh_cd)
    except Exception:
        LOG.exc('_start_if_still_selected failed')


def apply_now():
    """Popover button: apply the saved sets to the current vehicle immediately."""
    try:
        from CurrentVehicle import g_currentVehicle
        item = g_currentVehicle.item
        if item is None:
            return
        if _g_busy:
            _push_msg(t('alreadyRunning'), warning=True)
            return
        apply_sets(item.intCD)
    except Exception:
        LOG.exc('apply_now failed')


# --------------------------------------------------------------------------
# The apply run
# --------------------------------------------------------------------------

def _selection_changed(veh_cd):
    try:
        from CurrentVehicle import g_currentVehicle
        item = g_currentVehicle.item
        return item is None or item.intCD != veh_cd
    except Exception:
        return True


def _find_donor(want_cd, exclude_cd, donor_exclude=None):
    """First unlocked inventory vehicle (not in battle/queue/prebattle) that has
    the device in any of its setups. donor_exclude additionally rules out a
    whole set of vehicles — used in the batch run so Primary vehicles never
    cannibalize each other's just-installed equipment."""
    try:
        from gui.shared.utils.requesters import REQ_CRITERIA
        vehicles = _items_cache().items.getVehicles(REQ_CRITERIA.INVENTORY)
        for veh in vehicles.itervalues():
            if veh.intCD == exclude_cd:
                continue
            if donor_exclude and veh.intCD in donor_exclude:
                continue
            try:
                if not veh.optDevices.setupLayouts.containsIntCD(want_cd):
                    continue
                if veh.isLocked:
                    continue
            except Exception:
                continue
            return veh
    except Exception:
        LOG.exc('_find_donor failed')
    return None


def _locate_on_vehicle(vehicle, want_cd):
    """(setup_idx, slot_idx) of the device on the vehicle, preferring the active
    setup; (None, None) if not found."""
    layouts = vehicle.optDevices.setupLayouts
    active = layouts.layoutIndex
    indices = _setup_indices(vehicle)
    ordered = [active] + [i for i in indices if i != active]
    for setup_idx in ordered:
        cds = _setup_cds(vehicle, setup_idx)
        if want_cd in cds:
            return setup_idx, cds.index(want_cd)
    return None, None


def _obtainable_free(vehicle, veh_cd, item, donor_exclude=None):
    """True if the device can be sourced without spending anything: already on
    this vehicle, in the depot, or free-demountable from a donor vehicle."""
    try:
        if vehicle.optDevices.setupLayouts.containsIntCD(item.intCD):
            return True
        if item.inventoryCount > 0:
            return True
        if not _free_demount_ok(item):
            return False
        return _find_donor(item.intCD, veh_cd, donor_exclude) is not None
    except Exception:
        LOG.exc('_obtainable_free failed')
        return False


def _downgrade_item(vehicle, pink_item):
    """The standard (regular) counterpart of a special device that fits the
    vehicle, or None. Special and standard variants share the descriptor
    ARCHETYPE (e.g. both 'coatedOptics' or both 'improvedRotationMechanism') —
    groupName looked like the right key at first (trophyBasicCoatedOptics does
    declare a matching one) but most items leave it unset, in which case it
    defaults to the item's own unique internal name and never matches anything
    (this is why 'Bounty Rotation Mechanism' never found its standard sibling
    despite plenty being in stock). archetype has no such gap; it is always
    filled in for every tier/trophy/deluxe/modernized variant of a device.
    The vehicle filter selects the class matching this vehicle. Should several
    classes pass, the most expensive (best) one wins."""
    try:
        from gui.shared.gui_items import GUI_ITEM_TYPE
        from gui.shared.utils.requesters import REQ_CRITERIA
        archetype = pink_item.descriptor.archetype
        if not archetype:
            return None
        best = None
        best_price = -1
        devices = _items_cache().items.getItems(GUI_ITEM_TYPE.OPTIONALDEVICE, REQ_CRITERIA.EMPTY)
        for item in devices.itervalues():
            try:
                if not item.isRegular or item.descriptor.archetype != archetype:
                    continue
                ok, _ = item.descriptor.checkCompatibilityWithVehicle(vehicle.descriptor)
                if not ok:
                    continue
                price = 0
                try:
                    price = int(item.buyPrices.itemPrice.price.credits or 0)
                except Exception:
                    pass
            except Exception:
                continue
            if price > best_price:
                best, best_price = item, price
        return best
    except Exception:
        LOG.exc('_downgrade_item failed')
        return None


def _resolve_downgrades(vehicle, layout, veh_cd, donor_exclude=None):
    """Downgrade option: swap special devices (trophy/pink, bounty/modernized,
    deluxe) that cannot be sourced for free with their standard counterpart —
    but only when THAT one is sourceable for free itself, otherwise the normal
    skip flow reports the special device. isRegular is the same "plain
    standard device" check _downgrade_item uses to pick the replacement, so
    this covers every special category symmetrically.
    Returns (new layout, [(special name, standard name), ...])."""
    out = list(layout)
    notes = []
    for i, cd in enumerate(layout):
        if not cd:
            continue
        item = _fresh_item(cd)
        if item is None or getattr(item, 'isRegular', True):
            continue
        if _obtainable_free(vehicle, veh_cd, item, donor_exclude):
            continue
        alt = _downgrade_item(vehicle, item)
        if alt is None or alt.intCD in out:
            continue
        if not _obtainable_free(vehicle, veh_cd, alt, donor_exclude):
            continue
        out[i] = alt.intCD
        notes.append((item.userName, alt.userName))
    return out, notes


@adisp_async
@adisp_process
def _apply_one(veh_cd, watch_selection=True, force_downgrade=False,
               use_veil=True, push_summary=True, donor_exclude=None, callback=None):
    """Restore both saved sets onto one vehicle. All server operations run
    sequentially; every failure is recorded and the run continues with the next
    slot. Never spends money (see module docstring). Busy-state handling lives
    in the callers (apply_sets / equip_primary_vehicles); the outcome is
    reported via callback as a dict — `missing` lists the devices that could
    not be sourced for free."""
    installed_count = 0
    skipped = []    # (item name, reason)
    errors = []
    donated = []    # (item name, donor vehicle name)
    downgraded = []  # (trophy name, standard name)
    missing = []    # item names that could not be sourced at all
    original_idx = None
    veil_shown = False
    try:
        import mod_auto_equip

        saved = mod_auto_equip.get_sets(veh_cd)
        vehicle = _fresh_vehicle(veh_cd)
        if saved is None or vehicle is None:
            return
        if vehicle.isLocked:
            LOG.warning('apply_sets: vehicle %s is locked, aborting' % veh_cd)
            return
        original_idx = vehicle.optDevices.setupLayouts.layoutIndex
        available_setups = _setup_indices(vehicle)
        cap = _capacity(vehicle)
        plan = []
        if saved.get('set1') is not None and 0 in available_setups:
            plan.append((0, _norm_layout(saved['set1'], cap)))
        if saved.get('set2') is not None and 1 in available_setups:
            plan.append((1, _norm_layout(saved['set2'], cap)))
        if not plan:
            return
        # Downgrade BEFORE the no-op check: with the standard variant already
        # mounted, the resolved plan matches the vehicle and nothing runs —
        # otherwise every selection would demount and remount it.
        if force_downgrade or mod_auto_equip.is_downgrade_enabled():
            resolved = []
            for setup_idx, wanted in plan:
                wanted, notes = _resolve_downgrades(vehicle, wanted, veh_cd, donor_exclude)
                for note in notes:
                    if note not in downgraded:
                        downgraded.append(note)
                resolved.append((setup_idx, wanted))
            plan = resolved
        # Nothing to do → no veil flicker on every vehicle selection.
        if all(_setup_cds(vehicle, idx) == wanted for idx, wanted in plan):
            return
        LOG.info('apply_sets: start for %s, plan=%s' % (vehicle.userName, plan))
        if use_veil:
            veil_shown = _waiting_show()

        # Local depot-availability ledger for this run, immune to items-cache
        # resync lag: seeded once per cd from a fresh read, then updated by our
        # OWN successful demounts/installs. A cache re-read after a demount we
        # just performed (own vehicle or donor) can still show the OLD count —
        # the server ack landed, but the resync push hasn't caught up yet —
        # which previously made the money-guard below discard a perfectly
        # successful donor demount as "not available".
        avail = {}

        def _avail(cd):
            if cd not in avail:
                it = _fresh_item(cd)
                avail[cd] = it.inventoryCount if it is not None else 0
            return avail[cd]

        for setup_idx, wanted in plan:
            if _g_abort or (watch_selection and _selection_changed(veh_cd)):
                break
            vehicle = _fresh_vehicle(veh_cd)
            if vehicle is None:
                break
            cap = _capacity(vehicle)
            if _setup_cds(vehicle, setup_idx) == wanted:
                continue

            # Installs always go into the ACTIVE setup, so switch first.
            if vehicle.optDevices.setupLayouts.layoutIndex != setup_idx:
                code = yield _change_setup_index(vehicle.invID, setup_idx)
                if not _op_success(code):
                    errors.append(t('errSetupSwitchFailed', setup=setup_idx + 1, code=code))
                    continue
                yield _pause(_OP_PAUSE)

            # `final` is the layout the sequence command applies at the end of
            # this setup pass; slots we cannot serve fall back to their current
            # content (occupant kept) or stay empty.
            final = list(wanted)

            # ---- phase 1+2 per slot: clear occupants, secure availability ----
            for slot_idx in range(cap):
                if _g_abort or (watch_selection and _selection_changed(veh_cd)):
                    break
                vehicle = _fresh_vehicle(veh_cd)
                if vehicle is None:
                    break
                current = _setup_cds(vehicle, setup_idx)
                cur = current[slot_idx]
                want = final[slot_idx]
                if cur == want:
                    continue

                # Clear the slot first — but only if the device leaves this
                # setup entirely. If it merely moves to another slot of the
                # same setup, the sequence command repositions it for free
                # (native swapSlots + confirm works exactly like that), so no
                # demount round-trip is needed.
                if cur and cur not in wanted:
                    cur_item = _fresh_item(cur)
                    if cur_item is None:
                        errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=cur))
                        final[slot_idx] = cur
                        continue
                    other_indices = [i for i in _setup_indices(vehicle) if i != setup_idx]
                    in_other = any(vehicle.optDevices.setupLayouts.containsIntCD(cur, setupIdx=i) for i in other_indices)
                    if in_other:
                        # Stays on the vehicle (other setup) — free by definition.
                        code, ext = yield _equip_opt_device(vehicle.invID, 0, slot_idx, False, False)
                    else:
                        if not _free_demount_ok(cur_item):
                            skipped.append((cur_item.userName, t('reasonPaidDemount')))
                            final[slot_idx] = cur
                            continue
                        code, ext = yield _equip_opt_device(
                            vehicle.invID, 0, slot_idx, True, not cur_item.isRemovable)
                    if not _op_success(code):
                        errors.append(t('errDemountFailed', name=cur_item.userName, code=code, ext=ext))
                        final[slot_idx] = cur
                        continue
                    if not in_other:
                        avail[cur] = _avail(cur) + 1   # freed to depot
                    yield _pause(_OP_PAUSE)

                if not want:
                    continue

                # make the wanted device available (depot / this vehicle / donor)
                want_item = _fresh_item(want)
                if want_item is None:
                    errors.append(t('errUnknownItem', slot=slot_idx + 1, cd=want))
                    final[slot_idx] = 0
                    continue
                vehicle = _fresh_vehicle(veh_cd)
                on_vehicle = vehicle.optDevices.setupLayouts.containsIntCD(want)
                if not on_vehicle and _avail(want) <= 0:
                    if not _free_demount_ok(want_item):
                        skipped.append((want_item.userName, t('reasonDonorPaidDemount')))
                        missing.append(want_item.userName)
                        final[slot_idx] = 0
                        continue
                    donor = _find_donor(want, veh_cd, donor_exclude)
                    if donor is None:
                        skipped.append((want_item.userName, t('reasonNoDonor')))
                        missing.append(want_item.userName)
                        final[slot_idx] = 0
                        continue
                    d_setup, d_slot = _locate_on_vehicle(donor, want)
                    if d_slot is None:
                        skipped.append((want_item.userName, t('reasonNotFoundOnDonor', donor=donor.userName)))
                        missing.append(want_item.userName)
                        final[slot_idx] = 0
                        continue
                    d_original = donor.optDevices.setupLayouts.layoutIndex
                    if d_setup != d_original:
                        code = yield _change_setup_index(donor.invID, d_setup)
                        if not _op_success(code):
                            skipped.append((want_item.userName, t('reasonDonorSetupSwitchFailed', donor=donor.userName)))
                            final[slot_idx] = 0
                            continue
                        yield _pause(_OP_PAUSE)
                    LOG.info('demounting %s from %s (setup %d slot %d)' % (want_item.name, donor.userName, d_setup, d_slot))
                    code, ext = yield _equip_opt_device(
                        donor.invID, 0, d_slot, True, not want_item.isRemovable)
                    demount_ok = _op_success(code)
                    if demount_ok:
                        donated.append((want_item.userName, donor.userName))
                        avail[want] = _avail(want) + 1   # freed to depot
                    else:
                        errors.append(t('errDonorDemountFailed', name=want_item.userName, donor=donor.userName, code=code, ext=ext))
                    yield _pause(_OP_PAUSE)
                    if d_setup != d_original:
                        # No pause needed: nothing below reads donor state.
                        code = yield _change_setup_index(donor.invID, d_original)
                        if not _op_success(code):
                            LOG.warning('could not switch %s back to setup %d' % (donor.userName, d_original))
                    if not demount_ok:
                        final[slot_idx] = 0
                        continue

            # ---- phase 3: apply the whole setup layout in one command --------
            if _g_abort or (watch_selection and _selection_changed(veh_cd)):
                break
            vehicle = _fresh_vehicle(veh_cd)
            if vehicle is None:
                break
            current = _setup_cds(vehicle, setup_idx)
            # money guarantee: drop every NEW device that is not verifiably in
            # the depot or already on this vehicle — the sequence command would
            # buy it otherwise.
            for i in range(cap):
                cd = final[i]
                if not cd or cd == current[i]:
                    continue
                item = _fresh_item(cd)
                on_veh = vehicle.optDevices.setupLayouts.containsIntCD(cd)
                if item is None or (_avail(cd) <= 0 and not on_veh):
                    name = item.userName if item is not None else str(cd)
                    skipped.append((name, t('reasonNotAvailable')))
                    missing.append(name)
                    final[i] = current[i]
                elif not on_veh:
                    avail[cd] = _avail(cd) - 1   # consumed by this install
            if current != final:
                changes = sum(1 for i in range(cap) if final[i] and final[i] != current[i])
                code, err_str = yield _equip_opt_devs_sequence(vehicle.invID, final)
                if _op_success(code):
                    installed_count += changes
                else:
                    errors.append(t('errSetupApplyFailed', setup=setup_idx + 1, code=code, err=err_str))
                yield _pause(_OP_PAUSE)

        # ---- restore the originally active setup -------------------------
        vehicle = _fresh_vehicle(veh_cd)
        if vehicle is not None and original_idx is not None \
                and vehicle.optDevices.setupLayouts.layoutIndex != original_idx:
            code = yield _change_setup_index(vehicle.invID, original_idx)
            if not _op_success(code):
                LOG.warning('could not restore active setup %s on %s' % (original_idx, veh_cd))

        # ---- summary ------------------------------------------------------
        if push_summary and (installed_count or skipped or errors or donated):
            lines = []
            if installed_count:
                lines.append(t('summaryInstalled', count=installed_count))
            for pink_name, std_name in downgraded:
                lines.append(t('summaryDowngrade', special=pink_name, standard=std_name))
            for name, donor_name in donated:
                lines.append(t('summaryDonated', name=name, donor=donor_name))
            for name, reason in skipped:
                lines.append(t('summarySkipped', name=name, reason=reason))
            for err in errors:
                lines.append(t('summaryError', err=err))
            _push_msg(u'<br/>'.join(lines), warning=bool(skipped or errors))
        LOG.info('apply_sets: done for %s — installed=%d skipped=%d errors=%d'
                 % (veh_cd, installed_count, len(skipped), len(errors)))
    except Exception:
        LOG.exc('_apply_one failed')
    finally:
        if veil_shown:
            _waiting_hide()
        if callback is not None:
            callback({'installed': installed_count, 'skipped': skipped,
                      'errors': errors, 'donated': donated,
                      'downgraded': downgraded, 'missing': missing})


@adisp_process
def apply_sets(veh_cd):
    """Single-vehicle apply (selection trigger / popover button)."""
    global _g_busy, _g_abort
    if _g_busy:
        return
    _g_busy = True
    _g_abort = False
    _notify_refresh()
    try:
        yield _apply_one(veh_cd)
    except Exception:
        LOG.exc('apply_sets failed')
    finally:
        _g_busy = False
        _g_abort = False
        _notify_refresh()


def _filtered_primary_vehicles():
    """Favorite ('Primary') inventory vehicles that pass the current carousel
    filters. The filter state is read exactly like the hangar reads it: a
    BattlePassCarouselFilter loaded from the saved account settings. Falls back
    to all Primary vehicles if the filter cannot be built."""
    from gui.shared.utils.requesters import REQ_CRITERIA
    criteria = REQ_CRITERIA.INVENTORY | REQ_CRITERIA.VEHICLE.FAVORITE
    try:
        # same mode gate the random-hangar carousel uses (RANDOM_MODE_CRITERIA)
        criteria |= (~REQ_CRITERIA.VEHICLE.MODE_HIDDEN
                     | ~REQ_CRITERIA.VEHICLE.BATTLE_ROYALE
                     | ~REQ_CRITERIA.VEHICLE.EVENT_BATTLE
                     | REQ_CRITERIA.VEHICLE.ACTIVE_IN_NATION_GROUP)
    except Exception:
        LOG.exc('mode criteria unavailable — ignoring')
    try:
        from gui.filters.battle_pass_carousel_filter import BattlePassCarouselFilter
        flt = BattlePassCarouselFilter()
        flt.load()
        criteria |= flt.criteria
    except Exception:
        LOG.exc('carousel filter unavailable — using all Primary vehicles')
    try:
        vehicles = _items_cache().items.getVehicles(criteria)
        return sorted(vehicles.itervalues(), key=lambda v: (-v.level, v.userName))
    except Exception:
        LOG.exc('_filtered_primary_vehicles failed')
        return []


@adisp_process
def equip_primary_vehicles():
    """Popover button: equip ALL filtered Primary vehicles with their saved
    sets, trophy devices falling back to the standard variant. Posts one
    summary message plus — on shortage — an error message listing how many of
    which device are missing."""
    global _g_busy, _g_abort
    import mod_auto_equip
    if _g_busy:
        _push_msg(t('alreadyRunning'), warning=True)
        return
    targets = _filtered_primary_vehicles()
    if not targets:
        _push_msg(t('batchNoTargets'), warning=True)
        return
    with_sets = []
    without_sets = 0
    for veh in targets:
        saved = mod_auto_equip.get_sets(veh.intCD)
        if saved and (saved.get('set1') is not None or saved.get('set2') is not None):
            with_sets.append(veh)
        else:
            without_sets += 1
    if not with_sets:
        _push_msg(t('batchNoSavedSets', count=len(targets)), warning=True)
        return
    # Primary vehicles in this batch must never donate to each other — without
    # this, vehicle B (processed after A) would happily demount the very
    # device A just received, ping-ponging equipment instead of ending with
    # every Primary equipped. Only vehicles OUTSIDE this batch stay eligible
    # as donors.
    batch_cds = set(v.intCD for v in with_sets)
    _g_busy = True
    _g_abort = False
    _notify_refresh()
    veil_shown = _waiting_show()
    processed = 0
    total_installed = 0
    total_donated = 0
    all_downgraded = []
    all_errors = []
    missing_total = {}   # item name -> count
    try:
        LOG.info('equip_primary_vehicles: %d target(s): %s'
                 % (len(with_sets), [v.userName for v in with_sets]))
        for veh in with_sets:
            if _g_abort:
                break
            res = yield _apply_one(veh.intCD, watch_selection=False,
                                   force_downgrade=True, use_veil=False,
                                   push_summary=False, donor_exclude=batch_cds)
            processed += 1
            total_installed += res.get('installed', 0)
            total_donated += len(res.get('donated', []))
            for note in res.get('downgraded', []):
                if note not in all_downgraded:
                    all_downgraded.append(note)
            for err in res.get('errors', []):
                all_errors.append(t('batchVehicleError', veh=veh.userName, err=err))
            for name in res.get('missing', []):
                missing_total[name] = missing_total.get(name, 0) + 1

        lines = [t('batchSummary', processed=processed, installed=total_installed)]
        if total_donated:
            lines.append(t('batchDonated', count=total_donated))
        for pink_name, std_name in all_downgraded:
            lines.append(t('summaryDowngrade', special=pink_name, standard=std_name))
        if without_sets:
            lines.append(t('batchSkippedNoSets', count=without_sets))
        for err in all_errors[:8]:
            lines.append(t('summaryError', err=err))
        if len(all_errors) > 8:
            lines.append(t('batchMoreErrors', count=len(all_errors) - 8))
        _push_msg(u'<br/>'.join(lines), warning=bool(all_errors))

        if missing_total:
            miss_lines = [t('batchMissingHeader'), t('batchMissingListHeader')]
            for name in sorted(missing_total):
                miss_lines.append(t('batchMissingLine', count=missing_total[name], name=name))
            _push_msg(u'<br/>'.join(miss_lines), error=True)
        LOG.info('equip_primary_vehicles: done — processed=%d installed=%d missing=%s'
                 % (processed, total_installed, missing_total))
    except Exception:
        LOG.exc('equip_primary_vehicles failed')
    finally:
        if veil_shown:
            _waiting_hide()
        _g_busy = False
        _g_abort = False
        _notify_refresh()
