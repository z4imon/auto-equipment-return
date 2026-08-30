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

from .inventory import OPT_DEVICE_GROUP
from .log import LOG

# Pause between operations so the items cache settles before we read it again;
# the equip callbacks can fire before the resync has fully landed. Stale reads
# are safe-direction only: the server state is authoritative, so the worst case
# is a skipped install with a message - never a purchase.
OP_PAUSE = 0.01

_RES_COOLDOWN = -5      # AccountCommands.RES_COOLDOWN

# How long to wait before each retry. The first wait used to be 0.1s, and the
# baseline run showed what that costs: of 38 throttled calls, 27 needed a second
# retry and 5 a third, so the 0.1s attempt was usually thrown away. A retry is
# not free - it is a full round trip of ~370ms - so waiting 0.3s once is both
# faster and gentler than waiting 0.1s and then 0.2s. Every throttled call in
# that run was a setup switch; nothing else is rate-limited.
_COOLDOWN_DELAYS = (0.3, 0.3, 0.4, 0.5)


def is_success(code):
    return code >= 0


@adisp_async
def gather(steps, callback=None):
    """Fires several async steps AT ONCE and answers when the last one has,
    with their results in the order the steps were given.

    Nothing in the client serialises a server command: Account.__doCmd hands out
    a fresh requestID per call, parks the callback in a dict under it, and the
    base-entity RPC returns immediately. Responses come back through
    onCmdResponse(requestID) and are matched by that id, not by order. Waiting
    for one before sending the next is our own doing, not the client's.

    Only worth it for commands the server does not rate-limit. Measured over two
    sessions: change_setup came back RES_COOLDOWN on 27-48% of calls, but not one
    of 238 demounts ever did.

    `pending` is a list because Python 2 closures cannot rebind an outer name."""
    results = [None] * len(steps)
    if not steps:
        callback(results)
        return
    pending = [len(steps)]

    def done(index, result):
        results[index] = result
        pending[0] -= 1
        if pending[0] == 0:
            callback(results)

    for index, step in enumerate(steps):
        # index=index binds the value now; without it every closure would see
        # the last one.
        step(lambda result, index=index: done(index, result))


@adisp_async
def pause(seconds, callback=None):
    """The settle pause between operations."""
    def done():
        callback(None)

    BigWorld.callback(seconds, done)


def _retry_on_cooldown(fire, callback):
    """Runs an RPC and re-issues it while the server answers RES_COOLDOWN.
    `fire(done)` must issue the call and report back via done(code, result).
    The pause grows per attempt (0.1/0.2/0.3/0.4s); after that the cooldown
    result is passed through to the caller unchanged.

    `state` is a dict because Python 2 closures cannot rebind an outer name."""
    state = {'retries': 0}

    def attempt():
        def done(code, result):
            if code == _RES_COOLDOWN and state['retries'] < len(_COOLDOWN_DELAYS):
                delay = _COOLDOWN_DELAYS[state['retries']]
                state['retries'] += 1
                LOG.info('server cooldown, retrying in %.1fs (attempt %d)'
                         % (delay, state['retries']))
                BigWorld.callback(delay, attempt)
                return
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
    _retry_on_cooldown(fire, callback)


@adisp_async
def equip_device(veh_inv_id, device_cd, slot_idx, all_setups, finance_operation,
                 callback=None):
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
    _retry_on_cooldown(fire, callback)


@adisp_async
def apply_setup_layout(veh_inv_id, device_cds, callback=None):
    """Applies the WHOLE optional-device layout of the ACTIVE setup in one
    command (CMD_EQUIP_OPT_DEVS_SEQUENCE) - the native tank-setup confirm flow.
    It is the only server path that accepts a device currently mounted in the
    OTHER setup of the same vehicle; per-slot equip_device rejects that with
    RES_WRONG_ARGS (-2).

    IT DOES NOT DEMOUNT. Verified live: a layout that empties an occupied slot
    is refused WHOLESALE with RES_FAILURE (-1), "Demount of optional device must
    be performed in other command" - not per slot, the entire command fails. So
    a caller that could not source one device must send that slot's CURRENT
    content back rather than a 0, or it loses the whole setup. That is what
    apply._keep_what_could_not_be_replaced() is for.

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
    _retry_on_cooldown(fire, callback)
