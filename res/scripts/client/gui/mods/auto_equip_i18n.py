# -*- coding: utf-8 -*-
"""Two-language (de/en) text for every player-visible string in this mod: the
popover UI and every system message. One JSON file per language, sitting next
to the button icon (res/gui/maps/icons/z4imon/lang_<code>.json) — read once at
mod init through the game's own resource VFS, the same
ResMgr.openSection(path).asBinary + json.loads pattern the openwg_gameface base
mod uses for its own res_map.json. A .wotmod's contents are not a real
filesystem path, so plain open() can't reach them.

Internal/debug log lines (auto_equip_log.LOG.*) are deliberately NOT covered
here — they never reach the player, only the mod's own log file."""

import json

import ResMgr

from auto_equip_log import LOG

_LANG_PATH = u'gui/maps/icons/z4imon/lang_%s.json'
_SUPPORTED = ('de', 'en')
_FALLBACK = 'en'

_strings = {}
_language = _FALLBACK


def _read_lang_file(code):
    path = _LANG_PATH % code
    try:
        section = ResMgr.openSection(path)
        if section is None or not ResMgr.isFile(path):
            return None
        return json.loads(section.asBinary)
    except Exception:
        LOG.exc('failed to read language file %s' % path)
        return None


def _detect_language():
    try:
        from helpers import getClientLanguage
        code = getClientLanguage()
    except Exception:
        LOG.exc('getClientLanguage failed')
        code = _FALLBACK
    return code if code in _SUPPORTED else _FALLBACK


def init():
    """Loads the strings for the current client language. Call once, early in
    mod init — the client language cannot change without a game restart."""
    global _strings, _language
    _language = _detect_language()
    data = _read_lang_file(_language)
    if data is None and _language != _FALLBACK:
        LOG.warning('language file for "%s" missing/unreadable, falling back to "%s"'
                    % (_language, _FALLBACK))
        _language = _FALLBACK
        data = _read_lang_file(_language)
    _strings = data or {}
    LOG.info('loaded %d string(s) for language=%s' % (len(_strings), _language))


def ui_strings():
    """Flat dict pushed to the popover JS as uiJson (same shape the JS side
    already expects — only where the data now comes from changed)."""
    return dict(_strings)


def t(key, **kwargs):
    """One localized, formatted string. Missing key/file -> the raw key
    wrapped in !!...!! (loud on purpose: a silent fallback would hide a typo
    or missing key behind mixed-language text during testing)."""
    template = _strings.get(key)
    if template is None:
        LOG.warning('missing translation key: %s' % key)
        return u'!!%s!!' % key
    if kwargs:
        try:
            return template % kwargs
        except Exception:
            LOG.exc('failed to format key "%s" with %r' % (key, kwargs))
            return template
    return template
