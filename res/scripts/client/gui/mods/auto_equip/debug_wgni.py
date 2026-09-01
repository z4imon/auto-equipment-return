# -*- coding: utf-8 -*-
"""THROWAWAY diagnostic - not part of the equipment-sync feature itself.

Registers a ModsListAPI button that requests every native token type the
client's requestToken() supports (WGNI, WGNI_JWT, WOTG, WOTB) and logs each
one, so we can manually check whether any of them is usable as WG
account-ownership proof (fed into the server's existing account/info
verification, the same one the OAuth device-pairing flow already uses)
WITHOUT a browser-based login step at all.

WGNI alone was tried first and confirmed NOT usable (407 INVALID_ACCESS_TOKEN
against account/info) - see git history. This extends the same check to the
other three token types before concluding no native shortcut exists.

To check: click the button in-game, then open this mod's log
(python.log / the mod's own log output), find the "WGNI debug:" lines (one
per token type), and manually curl each one:

    https://api.worldoftanks.<eu|com|asia>/wot/account/info/
        ?application_id=<real app id>&account_id=<your real account id>
        &access_token=<the logged token>&fields=private.credits

If any of them comes back status=ok with a non-null "private" object, that
token is real, publicly-verifiable WG identity proof and we can skip the
whole browser flow. If all of them 407 (INVALID_ACCESS_TOKEN), none of the
client's native tokens work for this, and the OAuth device-pairing flow
already built for this feature is the way forward.

Delete this file and its one call site in mod_auto_equip.py once that
question is answered either way - see
docs/superpowers/specs/2026-08-31-equipment-cloud-sync-design.md.
"""

from constants import TOKEN_TYPE
from gui.shared.utils.requesters import getTokenRequester

from .log import LOG

_MODLIST_ID = 'z4imon.auto_equipment_return.debug_wgni'

_TOKEN_TYPES = [
    ('WGNI', TOKEN_TYPE.WGNI),
    ('WGNI_JWT', TOKEN_TYPE.WGNI_JWT),
    ('WOTG', TOKEN_TYPE.WOTG),
    ('WOTB', TOKEN_TYPE.WOTB),
]


def _make_callback(type_name):
    def _on_token_received(response):
        if response is None:
            LOG.step('WGNI debug: %s -> no response at all (request could not be sent)' % type_name)
            return
        if response.hasError():
            LOG.step('WGNI debug: %s -> error=%s' % (type_name, response.getError()))
            return
        if not response.isValid():
            LOG.step('WGNI debug: %s -> response present but not valid (empty/expired token)' % type_name)
            return
        LOG.step('WGNI debug: %s -> token=%s databaseID=%s'
                 % (type_name, response.getToken(), response.getDatabaseID()))
    return _on_token_received


def _on_click():
    LOG.step('WGNI debug: button clicked, requesting %d token type(s)' % len(_TOKEN_TYPES))
    for type_name, type_value in _TOKEN_TYPES:
        try:
            getTokenRequester(type_value).request(timeout=10.0)(_make_callback(type_name))
        except Exception:
            LOG.exc('WGNI debug: %s request failed' % type_name)


def register():
    try:
        from gui.modsListApi import g_modsListApi
    except Exception:
        LOG.info('WGNI debug: ModsListAPI not installed, skipping')
        return
    g_modsListApi.addModification(
        id=_MODLIST_ID, name='Debug: WGNI Token',
        description='Requests every native WG token type and logs each - temporary diagnostic, see debug_wgni.py.',
        enabled=True, login=False, lobby=True, callback=_on_click,
    )
