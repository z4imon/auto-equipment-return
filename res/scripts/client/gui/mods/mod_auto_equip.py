# -*- coding: utf-8 -*-
"""Mod entry point: the client calls init() and fini() here.

This module has to sit directly in gui.mods under a mod_ name - that is how
the client finds a mod at all. Everything else lives in the auto_equip package
next to it, one module per job:

    auto_equip.config      persisted settings and saved sets
    auto_equip.inventory   reads against the items cache
    auto_equip.rpc         raw inventory calls to the server
    auto_equip.apply       restoring saved sets onto vehicles
    auto_equip.save        snapshotting the current setups
    auto_equip.gameface    the hangar popover
    auto_equip.carousel_menu  the carousel right-click entry
    auto_equip.importer    the ModsSettingsAPI import panel
    auto_equip.i18n        player-visible strings
    auto_equip.messages    system messages and the hangar veil
"""

import os

from gui.shared.utils import getPlayerDatabaseID
from PlayerEvents import g_playerEvents

from .auto_equip import config, i18n, importer
from .auto_equip import __version__
from .auto_equip.log import LOG


def init():
    try:
        from .auto_equip import gameface
        i18n.init()
        gameface.init()
        if os.name == 'nt':
            _start_account_load()
        LOG.info('mod_auto_equip.init() finished OK')
    except Exception:
        LOG.exc('mod_auto_equip.init() failed')
    _carousel_menu('init')


def fini():
    try:
        from .auto_equip import gameface
        gameface.fini()
    except Exception:
        LOG.exc('mod_auto_equip.fini() failed')
    _carousel_menu('fini')


def _carousel_menu(step):
    """The carousel entry gets its own try block, outside the one above: it
    hooks a Scaleform class of the client, and losing that one extra menu item
    must never take the popover and the auto-install down with it."""
    try:
        from .auto_equip import carousel_menu
        getattr(carousel_menu, step)()
    except Exception:
        LOG.exc('carousel menu %s() failed' % step)


# ---------------------------------------------------------------------------
# Account load
#
# The config - and the import panel it seeds - need a real account id, but
# init() runs before login, so wait for one when it isn't there yet.
# ---------------------------------------------------------------------------

def _current_account_id():
    """The WoT account databaseID, or 0 if it isn't known yet."""
    try:
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
    g_playerEvents.onAccountShowGUI += _on_account_show_gui


def _on_account_show_gui(ctx=None):
    g_playerEvents.onAccountShowGUI -= _on_account_show_gui
    _finish_account_load(_current_account_id())


def _finish_account_load(account_id):
    config.load_for_account(account_id)
    try:
        importer.register(account_id)
    except Exception:
        LOG.exc('auto_equip.importer.register failed')
