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
_language = _FALLBACK
# The English file, kept alongside whatever language actually loaded. Used only
# for keys the player's language does not have: translations and code changes
# ship in the same .wotmod, so a key added to the mod is missing from every
# language file except the ones that were translated with it. Without this
# fallback such a key reaches the player as the raw "!!key!!" marker below,
# which is meant to flag a typo during development, not to be shipped.
_fallback_strings = {}


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
    global _strings, _language, _fallback_strings
    _language = _detect_language()
    data = _read_lang_file(_language)
    if data is None and _language != _FALLBACK:
        LOG.warning('language file for "%s" missing/unreadable, falling back to "%s"'
                    % (_language, _FALLBACK))
        _language = _FALLBACK
        data = _read_lang_file(_language)
    _strings = data or {}
    if _language == _FALLBACK:
        _fallback_strings = _strings
    else:
        _fallback_strings = _read_lang_file(_FALLBACK) or {}
    LOG.info('loaded %d string(s) for language=%s (%d in the English fallback)'
             % (len(_strings), _language, len(_fallback_strings)))


def language():
    """The language code actually loaded, after the fallback was applied."""
    return _language


# Popover keys that show the game's own wording instead of the language file:
# the mode switch's first field reads exactly like the battle type menu
# ("Zufallsgefecht", "Random Battle", ...). The file's value stays as fallback.
_GAME_STRINGS = {
    'profileStandard': '#menu:headerButtons/battle/types/standart',
}


def _game_string(key):
    """The client's translation of key, or None when it has none."""
    try:
        from helpers import i18n as game_i18n
        text = game_i18n.makeString(key)
    except Exception:
        LOG.exc('makeString(%s) failed' % key)
        return None
    if not text or text == key or text.startswith('#'):
        return None
    return text


def _lookup(key):
    """The value of `key`, chasing `@otherKey` aliases. None when neither the
    loaded language nor the English fallback has it."""
    for source in (_strings, _fallback_strings):
        template = source.get(key)
        seen = set([key])
        while (template is not None and isinstance(template, basestring)
                and len(template) > 1 and template[0] == '@'):
            ref = template[1:]
            if ref in seen:
                LOG.warning('translation alias cycle at %s -> %s' % (key, ref))
                template = None
                break
            seen.add(ref)
            template = source.get(ref)
        if template is not None:
            return template
    return None


def ui_strings():
    """Flat dict pushed to the popover JS as uiJson (same shape the JS side
    already expects — only where the data now comes from changed).

    Every key either file knows is included, the loaded language winning, so a
    key that only exists in English still reaches the JS instead of falling
    back to the hardcoded English default in the JS itself."""
    strings = dict(_fallback_strings)
    strings.update(_strings)
    for name, key in _GAME_STRINGS.items():
        text = _game_string(key)
        if text:
            strings[name] = text
    return strings


def t(key, **kwargs):
    """One localized, formatted string.

    A key missing from the player's language falls back to the English file;
    one missing from both returns the raw key wrapped in !!...!!, which is loud
    on purpose: during development that should hide nothing. That marker is not
    meant to reach players, which is exactly what the English fallback is for -
    translations and code ship together, so a newly added key is absent from
    every language file nobody translated it into yet.

    A value of `@otherKey` reuses that other entry in the same language file
    so duplicated menu labels do not have to be copied by hand."""
    template = _lookup(key)
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
