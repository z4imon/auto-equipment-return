# -*- coding: utf-8 -*-
"""THROWAWAY diagnostic - not part of the equipment-sync feature itself.

Registers a ModsListAPI button that requests a WGNI token (TOKEN_TYPE.WGNI)
via the client's native requestToken() and logs it, so we can manually check
whether it's usable as WG account-ownership proof (fed into the server's
existing account/info verification, the same one the OAuth device-pairing
flow already uses) WITHOUT a browser-based login step at all.

To check: click the button in-game, then open this mod's log
(python.log / the mod's own log output), find the line starting
"WGNI debug:", and manually curl:

    https://api.worldoftanks.<eu|com|asia>/wot/account/info/
        ?application_id=<real app id>&account_id=<your real account id>
        &access_token=<the logged token>&fields=private.credits

If it comes back status=ok with a non-null "private" object, the token is a
real, publicly-verifiable WG access_token and we can skip the whole browser
flow. If it 407s (INVALID_ACCESS_TOKEN), it isn't, and we're back to the
OAuth device-pairing flow already built for this feature.

Delete this file and its one call site in mod_auto_equip.py once that
question is answered either way - see
docs/superpowers/specs/2026-08-31-equipment-cloud-sync-design.md.
"""

from constants import TOKEN_TYPE
from gui.shared.utils.requesters import getTokenRequester

from .log import LOG

_MODLIST_ID = 'z4imon.auto_equipment_return.debug_wgni'


def _on_token_received(response):
    if response is None:
        LOG.step('WGNI debug: no response at all (request could not be sent)')
        return
    if response.hasError():
        LOG.step('WGNI debug: error=%s' % response.getError())
        return
    if not response.isValid():
        LOG.step('WGNI debug: response present but not valid (empty/expired token)')
        return
    LOG.step('WGNI debug: token=%s databaseID=%s' % (response.getToken(), response.getDatabaseID()))


def _on_click():
    LOG.step('WGNI debug: button clicked, requesting token')
    try:
        getTokenRequester(TOKEN_TYPE.WGNI).request(timeout=10.0)(_on_token_received)
    except Exception:
        LOG.exc('WGNI debug: request failed')


def register():
    try:
        from gui.modsListApi import g_modsListApi
    except Exception:
        LOG.info('WGNI debug: ModsListAPI not installed, skipping')
        return
    g_modsListApi.addModification(
        id=_MODLIST_ID, name='Debug: WGNI Token',
        description='Requests a WGNI token and logs it - temporary diagnostic, see debug_wgni.py.',
        enabled=True, login=False, lobby=True, callback=_on_click,
    )
