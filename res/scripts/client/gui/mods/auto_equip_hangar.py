# -*- coding: utf-8 -*-
"""Which hangar the player is currently looking at.

Besides the standard hangar, several game modes bring one of their own. They
matter to this mod twice over: the popover has to be injected into each of
them, and "equip all Primary vehicles" has to work on the vehicles THAT hangar
shows - a mode carousel lists a different set, filtered by a different saved
filter, than the random one.

Both users of that knowledge (auto_equip_gameface for the injection,
auto_equip_inventory for the vehicle query) would otherwise have to import each
other, so the mapping lives here on its own.

A mode is identified by its script package name, which is also the prefix of
its layout resource (R.views.<package>.mono.lobby.hangar). The standard hangar
is represented by None.
"""

from gui.impl.gen import R

from auto_equip_log import LOG

STANDARD = None

MODE_PACKAGES = (
    'comp7',            # Onslaught
    'comp7_light',      # Onslaught light
    'frontline',        # Frontline
    'last_stand',       # Last Stand
    'fun_random',       # Arcade Cabinet
)

_STANDARD_LAYOUT_ID = R.views.mono.hangar.main()

# package -> layout id, filled in lazily by _mode_layout_ids(). Modes register
# their layouts at runtime, so anything still missing is retried on the next
# call rather than written off: the first hangar can load before a mode's
# package is up. A mode this client doesn't ship simply never resolves.
_layout_ids = {}

# The hangar the player is in right now, as of the last set_active() call.
_active_mode = STANDARD


def _mode_layout_ids():
    for package in MODE_PACKAGES:
        if package in _layout_ids:
            continue
        try:
            _layout_ids[package] = getattr(R.views, package).mono.lobby.hangar()
            LOG.info('resolved the %s hangar layout' % package)
        except Exception:
            pass        # not registered (yet), or not part of this client
    return _layout_ids


def is_hangar(layout_id):
    return layout_id == _STANDARD_LAYOUT_ID or layout_id in _mode_layout_ids().values()


def mode_of(layout_id):
    """The mode package a hangar layout belongs to, or STANDARD for the normal
    hangar. Only meaningful for ids that pass is_hangar()."""
    for package, mode_layout_id in _mode_layout_ids().iteritems():
        if layout_id == mode_layout_id:
            return package
    return STANDARD


def set_active(layout_id):
    global _active_mode
    _active_mode = mode_of(layout_id)
    LOG.info('active hangar: %s' % (_active_mode or 'standard'))


def active_mode():
    return _active_mode
