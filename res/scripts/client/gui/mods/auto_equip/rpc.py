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
_COOLDOWN_RETRIES = 4


def is_success(code):
    return code >= 0


@adisp_async
def pause(seconds, callback=None):
    BigWorld.callback(seconds, lambda: callback(None))


def _retry_on_cooldown(fire, callback, attempt=0):
    """Runs an RPC and re-issues it while the server answers RES_COOLDOWN.
    `fire(done)` must issue the call and report back via done(code, result).
    The pause grows per attempt (0.1/0.2/0.3/0.4s); after that the cooldown
    result is passed through to the caller unchanged."""
    def done(code, result):
        if code == _RES_COOLDOWN and attempt < _COOLDOWN_RETRIES:
            delay = 0.1 * (attempt + 1)
            LOG.info('server cooldown, retrying in %.1fs (attempt %d)' % (delay, attempt + 1))
            BigWorld.callback(delay, lambda: _retry_on_cooldown(fire, callback, attempt + 1))
        else:
            callback(result)
    fire(done)


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
    _retry_on_cooldown(fire, callback)


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
    _retry_on_cooldown(fire, callback)
