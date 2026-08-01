# -*- coding: utf-8 -*-
"""Two-language (de/en) text for every player-visible string in this mod: the
popover UI and every system message. One JSON file per language, sitting next
to the button icon (res/gui/maps/icons/z4imon/lang_<code>.json) — read once at
mod init through the game's own resource VFS, the same
ResMgr.openSection(path).asBinary + json.loads pattern the openwg_gameface base
mod uses for its own res_map.json. A .wotmod's contents are not a real
filesystem path, so plain open() can't reach them.

Internal/debug log lines (log.LOG.*) are deliberately NOT covered
here — they never reach the player, only the mod's own log file."""

import json

import ResMgr

from helpers import getClientLanguage

from .log import LOG

_LANG_PATH = u'gui/maps/icons/z4imon/lang_%s.json'
_SUPPORTED = ('be', 'bg', 'cs', 'de', 'el', 'en', 'es', 'es_ar', 'et', 'fi',
              'fr', 'hr', 'hu', 'it', 'ja', 'kk', 'lt', 'lv', 'no', 'pl',
              'pt', 'pt_br', 'ro', 'ru', 'sv', 'tr', 'uk', 'zh_cn')
_FALLBACK = 'en'

_strings = {}
_fallback_strings = {}      # English, for keys the chosen language has no entry for
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
        code = getClientLanguage()
    except Exception:
        LOG.exc('getClientLanguage failed')
        code = _FALLBACK
    return code if code in _SUPPORTED else _FALLBACK


def init():
    """Loads the strings for the current client language. Call once, early in
    mod init — the client language cannot change without a game restart."""
    global _strings, _fallback_strings, _language
    _language = _detect_language()
    data = _read_lang_file(_language)
    if data is None and _language != _FALLBACK:
        LOG.warning('language file for "%s" missing/unreadable, falling back to "%s"'
                    % (_language, _FALLBACK))
        _language = _FALLBACK
        data = _read_lang_file(_language)
    _strings = data or {}
    _fallback_strings = (_strings if _language == _FALLBACK
                         else _read_lang_file(_FALLBACK) or {})
    LOG.info('loaded %d string(s) for language=%s' % (len(_strings), _language))


def ui_strings():
    """Flat dict pushed to the popover JS as uiJson (same shape the JS side
    already expects — only where the data now comes from changed)."""
    merged = dict(_fallback_strings)
    merged.update(_strings)
    return merged


def _template(key):
    """The string for a key in the client's language, or the English one when
    that language does not have it (yet).

    With 28 language files, a newly added string realistically lands in one or
    two of them first. Showing the rest !!theKey!! is worse than showing them
    English: the player can still read what the button does, and the log line
    says which file is missing what."""
    template = _strings.get(key)
    if template is not None:
        return template
    template = _fallback_strings.get(key)
    if template is not None:
        LOG.warning('key "%s" is missing from lang_%s.json, using %s'
                    % (key, _language, _FALLBACK))
    return template


def t(key, **kwargs):
    """One localized, formatted string. A key missing from every language file ->
    the raw key wrapped in !!...!! (loud on purpose: a silent fallback would hide
    a typo behind plausible-looking text during testing)."""
    template = _template(key)
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
