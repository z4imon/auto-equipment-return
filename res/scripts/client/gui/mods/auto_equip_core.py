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
_OP_PAUSE = 0.1

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


def _push_msg(text, warning=False):
    try:
        from gui import SystemMessages
        sm_type = SystemMessages.SM_TYPE.Warning if warning else SystemMessages.SM_TYPE.Information
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
        return u'Kein Fahrzeug ausgewählt'
    snap = snapshot_sets(vehicle)
    set1 = snap['set1'] if which in (1, 3) else None
    set2 = snap['set2'] if which in (2, 3) else None
    if which == 2 and snap['set2'] is None:
        return u'Dieses Fahrzeug hat kein zweites Loadout'
    mod_auto_equip.store_sets(vehicle.intCD, set1=set1, set2=set2)
    LOG.info('saved sets for %s (which=%s): set1=%s set2=%s' % (vehicle.userName, which, set1, set2))
    if which == 1:
        return u'Set 1 gespeichert'
    if which == 2:
        return u'Set 2 gespeichert'
    return u'Beide Sets gespeichert'


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
            _push_msg(u'AutoEquip: Einbau läuft bereits', warning=True)
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


def _find_donor(want_cd, exclude_cd):
    """First unlocked inventory vehicle (not in battle/queue/prebattle) that has
    the device in any of its setups."""
    try:
        from gui.shared.utils.requesters import REQ_CRITERIA
        vehicles = _items_cache().items.getVehicles(REQ_CRITERIA.INVENTORY)
        for veh in vehicles.itervalues():
            if veh.intCD == exclude_cd:
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


@adisp_process
def apply_sets(veh_cd):
    """Restore both saved sets onto the vehicle. All server operations run
    sequentially; every failure is recorded and the run continues with the next
    slot. Never spends money (see module docstring)."""
    global _g_busy, _g_abort
    if _g_busy:
        return
    _g_busy = True
    _g_abort = False
    _notify_refresh()
    installed_count = 0
    skipped = []    # (item name, reason)
    errors = []
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
        # Nothing to do → no veil flicker on every vehicle selection.
        if all(_setup_cds(vehicle, idx) == wanted for idx, wanted in plan):
            return
        LOG.info('apply_sets: start for %s, plan=%s' % (vehicle.userName, plan))
        veil_shown = _waiting_show()

        for setup_idx, wanted in plan:
            if _g_abort or _selection_changed(veh_cd):
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
                    errors.append(u'Setup %d nicht umschaltbar (Code %s)' % (setup_idx + 1, code))
                    continue
                yield _pause(_OP_PAUSE)

            # `final` is the layout the sequence command applies at the end of
            # this setup pass; slots we cannot serve fall back to their current
            # content (occupant kept) or stay empty.
            final = list(wanted)

            # ---- phase 1+2 per slot: clear occupants, secure availability ----
            for slot_idx in range(cap):
                if _g_abort or _selection_changed(veh_cd):
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
                        errors.append(u'Slot %d: unbekanntes Item %s' % (slot_idx + 1, cur))
                        final[slot_idx] = cur
                        continue
                    other_indices = [i for i in _setup_indices(vehicle) if i != setup_idx]
                    in_other = any(vehicle.optDevices.setupLayouts.containsIntCD(cur, setupIdx=i) for i in other_indices)
                    if in_other:
                        # Stays on the vehicle (other setup) — free by definition.
                        code, ext = yield _equip_opt_device(vehicle.invID, 0, slot_idx, False, False)
                    else:
                        if not _free_demount_ok(cur_item):
                            skipped.append((cur_item.userName, u'Ausbau wäre kostenpflichtig'))
                            final[slot_idx] = cur
                            continue
                        code, ext = yield _equip_opt_device(
                            vehicle.invID, 0, slot_idx, True, not cur_item.isRemovable)
                    if not _op_success(code):
                        errors.append(u'%s: Ausbau fehlgeschlagen (Code %s, %s)' % (cur_item.userName, code, ext))
                        final[slot_idx] = cur
                        continue
                    yield _pause(_OP_PAUSE)

                if not want:
                    continue

                # make the wanted device available (depot / this vehicle / donor)
                want_item = _fresh_item(want)
                if want_item is None:
                    errors.append(u'Slot %d: unbekanntes Item %s' % (slot_idx + 1, want))
                    final[slot_idx] = 0
                    continue
                vehicle = _fresh_vehicle(veh_cd)
                on_vehicle = vehicle.optDevices.setupLayouts.containsIntCD(want)
                if not on_vehicle and want_item.inventoryCount <= 0:
                    if not _free_demount_ok(want_item):
                        skipped.append((want_item.userName, u'Ausbau vom anderen Panzer wäre kostenpflichtig'))
                        final[slot_idx] = 0
                        continue
                    donor = _find_donor(want, veh_cd)
                    if donor is None:
                        skipped.append((want_item.userName, u'nicht im Lager und kein freier Panzer hat es'))
                        final[slot_idx] = 0
                        continue
                    d_setup, d_slot = _locate_on_vehicle(donor, want)
                    if d_slot is None:
                        skipped.append((want_item.userName, u'auf %s nicht auffindbar' % donor.userName))
                        final[slot_idx] = 0
                        continue
                    d_original = donor.optDevices.setupLayouts.layoutIndex
                    if d_setup != d_original:
                        code = yield _change_setup_index(donor.invID, d_setup)
                        if not _op_success(code):
                            skipped.append((want_item.userName, u'Setupwechsel auf %s fehlgeschlagen' % donor.userName))
                            final[slot_idx] = 0
                            continue
                        yield _pause(_OP_PAUSE)
                    LOG.info('demounting %s from %s (setup %d slot %d)' % (want_item.name, donor.userName, d_setup, d_slot))
                    code, ext = yield _equip_opt_device(
                        donor.invID, 0, d_slot, True, not want_item.isRemovable)
                    demount_ok = _op_success(code)
                    if not demount_ok:
                        errors.append(u'%s: Ausbau von %s fehlgeschlagen (Code %s, %s)' % (want_item.userName, donor.userName, code, ext))
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
            if _g_abort or _selection_changed(veh_cd):
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
                if item is None or (item.inventoryCount <= 0
                                    and not vehicle.optDevices.setupLayouts.containsIntCD(cd)):
                    name = item.userName if item is not None else str(cd)
                    skipped.append((name, u'nicht verfügbar — Einbau übersprungen'))
                    final[i] = current[i]
            if current != final:
                changes = sum(1 for i in range(cap) if final[i] and final[i] != current[i])
                code, err_str = yield _equip_opt_devs_sequence(vehicle.invID, final)
                if _op_success(code):
                    installed_count += changes
                else:
                    errors.append(u'Setup %d: Einbau fehlgeschlagen (Code %s, %s)' % (setup_idx + 1, code, err_str))
                yield _pause(_OP_PAUSE)

        # ---- restore the originally active setup -------------------------
        vehicle = _fresh_vehicle(veh_cd)
        if vehicle is not None and original_idx is not None \
                and vehicle.optDevices.setupLayouts.layoutIndex != original_idx:
            code = yield _change_setup_index(vehicle.invID, original_idx)
            if not _op_success(code):
                LOG.warning('could not restore active setup %s on %s' % (original_idx, veh_cd))

        # ---- summary ------------------------------------------------------
        if installed_count or skipped or errors:
            lines = []
            if installed_count:
                lines.append(u'AutoEquip: %d Teil(e) eingebaut' % installed_count)
            for name, reason in skipped:
                lines.append(u'Übersprungen: %s — %s' % (name, reason))
            for err in errors:
                lines.append(u'Fehler: %s' % err)
            _push_msg(u'<br/>'.join(lines), warning=bool(skipped or errors))
        LOG.info('apply_sets: done for %s — installed=%d skipped=%d errors=%d'
                 % (veh_cd, installed_count, len(skipped), len(errors)))
    except Exception:
        LOG.exc('apply_sets failed')
    finally:
        if veil_shown:
            _waiting_hide()
        _g_busy = False
        _g_abort = False
        _notify_refresh()
