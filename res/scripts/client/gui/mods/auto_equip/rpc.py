# -*- coding: utf-8 -*-
"""The raw inventory calls this mod makes to the game server, wrapped as adisp
async steps so the apply run can `yield` them one after another.

We deliberately do NOT use the GUI processor classes (OptDeviceInstaller,
ChangeVehicleSetupEquipments): their constructors eagerly resolve R.strings for
confirm dialogs and crash on the live client, where those strings changed
(AttributeError: 'dialogs' object has no attribute 'confirmationNotRemovable').
These are the same server calls the processors issue in _request(); the server
validates everything and answers with a negative code on failure.

Because these are the raw calls, no confirm dialog ever appears - which is
exactly why the caller must do its own money checks before installing
anything. See apply.py's money guarantee.
"""

import BigWorld

from adisp import adisp_async

from . import metrics
from .inventory import OPT_DEVICE_GROUP
from .log import LOG

# Pause between operations so the items cache settles before we read it again;
# the equip callbacks can fire before the resync has fully landed. Stale reads
# are safe-direction only: the server state is authoritative, so the worst case
# is a skipped install with a message - never a purchase.
OP_PAUSE = 0.01

_RES_COOLDOWN = -5      # AccountCommands.RES_COOLDOWN
_COOLDOWN_RETRIES = 4


def is_success(code):
    return code >= 0


@adisp_async
def pause(seconds, callback=None):
    """The settle pause between operations. Booked on its own metrics counter:
    it is neither server time nor our own work, and a run that turns out to be
    mostly OP_PAUSE is a very different problem from one that is mostly latency."""
    started = metrics.now()

    def done():
        metrics.note_pause(metrics.elapsed_ms(started))
        callback(None)

    BigWorld.callback(seconds, done)


def _retry_on_cooldown(op, fields, fire, callback):
    """Runs an RPC and re-issues it while the server answers RES_COOLDOWN.
    `fire(done)` must issue the call and report back via done(code, result).
    The pause grows per attempt (0.1/0.2/0.3/0.4s); after that the cooldown
    result is passed through to the caller unchanged.

    This is also where every server call in the mod is timed - all three RPCs
    funnel through here, so one measuring point covers them all. The reported
    duration is server time ONLY: the retry sleeps are subtracted out and
    reported as backoff_ms, otherwise the run summary would count them twice,
    once as server time and once as backoff.

    `state` is a dict because Python 2 closures cannot rebind an outer name."""
    started = metrics.now()
    state = {'retries': 0, 'backoff_ms': 0.0}

    def attempt():
        def done(code, result):
            if code == _RES_COOLDOWN and state['retries'] < _COOLDOWN_RETRIES:
                delay = 0.1 * (state['retries'] + 1)
                state['retries'] += 1
                state['backoff_ms'] += delay * 1000.0
                LOG.info('server cooldown, retrying in %.1fs (attempt %d)'
                         % (delay, state['retries']))
                BigWorld.callback(delay, attempt)
                return
            metrics.note_op(op, metrics.elapsed_ms(started) - state['backoff_ms'],
                            code=code, retries=state['retries'],
                            backoff_ms=state['backoff_ms'], **fields)
            callback(result)
        fire(done)

    attempt()


@adisp_async
def change_setup_index(veh_inv_id, setup_idx, callback=None):
    """Makes `setup_idx` the vehicle's active optional-device setup.
    Reports the plain server code."""
    def fire(done):
        try:
            BigWorld.player().inventory.changeVehicleSetupGroup(
                veh_inv_id, OPT_DEVICE_GROUP, setup_idx, lambda code: done(code, code))
        except Exception:
            LOG.exc('change_setup_index RPC failed')
            done(-1, -1)
    _retry_on_cooldown('change_setup', {'veh_inv_id': veh_inv_id,
                                        'setup_idx': setup_idx}, fire, callback)


@adisp_async
def equip_device(veh_inv_id, device_cd, slot_idx, all_setups, finance_operation, callback=None):
    """Installs one device into one slot, or demounts it when device_cd is 0.
    Reports (code, extra). Never passes useDemountKit - demount kits must not
    be spent."""
    def fire(done):
        def on_response(code, extra=None):
            done(code, (code, extra))
        try:
            BigWorld.player().inventory.equipOptionalDevice(
                veh_inv_id, device_cd, slot_idx, all_setups, finance_operation,
                on_response, False)
        except Exception:
            LOG.exc('equip_device RPC failed')
            done(-1, (-1, None))
    # Three different jobs share one server call, and they cost the player very
    # different things - so they get three different op names. 'demount' sends
    # the device to the depot (all_setups), 'unslot' only frees the slot in the
    # active setup because the device stays mounted in the other one.
    if device_cd:
        op = 'equip'
    else:
        op = 'demount' if all_setups else 'unslot'
    _retry_on_cooldown(op, {'veh_inv_id': veh_inv_id, 'slot_idx': slot_idx,
                            'device_cd': device_cd}, fire, callback)


@adisp_async
def apply_setup_layout(veh_inv_id, device_cds, callback=None):
    """Applies the WHOLE optional-device layout of the ACTIVE setup in one
    command (CMD_EQUIP_OPT_DEVS_SEQUENCE) - the native tank-setup confirm flow.
    It is the only server path that accepts a device currently mounted in the
    OTHER setup of the same vehicle; per-slot equip_device rejects that with
    RES_WRONG_ARGS (-2).

    CAUTION: this is the buy-and-install command. Callers must have verified
    that every listed device is in the depot or already on the vehicle, or the
    server will happily buy the missing ones. Reports (code, error string)."""
    cds = [int(cd) for cd in device_cds]

    def fire(done):
        def on_response(code, error='', extra=None):
            done(code, (code, error))
        try:
            BigWorld.player().inventory.equipOptDevsSequence(veh_inv_id, cds, on_response)
        except Exception:
            LOG.exc('apply_setup_layout RPC failed')
            done(-1, (-1, 'rpc failed'))
    # n_slots is how many slots this ONE command set - the number that shows
    # whether a later build really replaced single calls with batched ones.
    _retry_on_cooldown('layout', {'veh_inv_id': veh_inv_id,
                                  'n_slots': len(cds)}, fire, callback)
