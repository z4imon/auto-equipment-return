# -*- coding: utf-8 -*-
"""Mod entry point: the client calls init() and fini() here.

Its only job is bringing the pieces up in the right order. Everything else
lives in its own module:

    auto_equip_config      persisted settings and saved sets
    auto_equip_inventory   reads against the items cache
    auto_equip_rpc         raw inventory calls to the server
    auto_equip_apply       restoring saved sets onto vehicles
    auto_equip_save        snapshotting the current setups
    auto_equip_gameface    the hangar popover
    auto_equip_import      the ModsSettingsAPI import panel
    auto_equip_i18n        player-visible strings
    auto_equip_messages    system messages and the hangar veil
"""

import os

import auto_equip_config as config
from auto_equip_log import LOG


def init():
    try:
        import auto_equip_i18n
        import auto_equip_gameface
        auto_equip_i18n.init()
        auto_equip_gameface.init()
        if os.name == 'nt':
            _start_account_load()
        LOG.info('mod_auto_equip.init() finished OK')
    except Exception:
        LOG.exc('mod_auto_equip.init() failed')


def fini():
    try:
        import auto_equip_gameface
        auto_equip_gameface.fini()
    except Exception:
        LOG.exc('mod_auto_equip.fini() failed')


# ---------------------------------------------------------------------------
# Account load
#
# The config - and the import panel it seeds - need a real account id, but
# init() runs before login, so wait for one when it isn't there yet.
# ---------------------------------------------------------------------------

def _current_account_id():
    """The WoT account databaseID, or 0 if it isn't known yet."""
    try:
        from gui.shared.utils import getPlayerDatabaseID
        return getPlayerDatabaseID() or 0
    except Exception:
        LOG.exc('could not read the account id')
        return 0


def _start_account_load():
    account_id = _current_account_id()
    if account_id:
        _finish_account_load(account_id)
        return
    LOG.info('account id not ready yet, waiting for onAccountShowGUI')
    from PlayerEvents import g_playerEvents
    g_playerEvents.onAccountShowGUI += _on_account_show_gui


def _on_account_show_gui(ctx=None):
    from PlayerEvents import g_playerEvents
    g_playerEvents.onAccountShowGUI -= _on_account_show_gui
    _finish_account_load(_current_account_id())


def _finish_account_load(account_id):
    config.load_for_account(account_id)
    try:
        import auto_equip_import
        auto_equip_import.register(account_id)
    except Exception:
        LOG.exc('auto_equip_import.register failed')
