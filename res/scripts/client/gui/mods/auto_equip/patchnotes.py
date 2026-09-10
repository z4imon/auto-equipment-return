# -*- coding: utf-8 -*-
"""What changed in the versions the player has not been told about yet.

Deliberately NOT an update check: nothing is fetched, there is no server and no
comparison against anything remote. The notes ship inside the mod, so whoever
has this build has its notes - which is the only thing that reaches players who
are updated for them by a modpack.

Two decisions worth knowing:

* Entries are a LIST, not a dict keyed by version. json.loads() on Python 2.7
  returns an unordered dict, so a dict could not carry the order the notes must
  be shown in. The file's order is the display order, newest first.
* Which entries were shown is remembered in this module's OWN file, not in the
  account config. It is one file per installation rather than per account, and
  the account config is rewritten on every settings change - a marker living
  there could not be edited while the game is running.
"""

import json
import os

import ResMgr

from gui.shared.formatters import text_styles
from gui.shared.notifications import NotificationPriorityLevel

from . import config, i18n, messages
from .i18n import t
from .log import LOG

_NOTES_PATH = u'gui/maps/icons/z4imon/patchnotes.json'
_FALLBACK_LANGUAGE = 'en'
_BULLET = u'• '


def _seen_file():
    """Account-independent, and outside account_files_dir() on purpose: the
    importer lists every *.json in there as an importable account."""
    return os.path.join(config.mods_dir(), 'z4imon', 'AutoEquipPatchNotes.json')


def _read_seen():
    """Ids already shown. Missing or unreadable file means "nothing shown yet",
    which only ever costs one repeat - never a lost notification."""
    path = _seen_file()
    try:
        if not os.path.exists(path):
            return []
        with open(path, 'r') as handle:
            data = json.load(handle)
        shown = data.get('shown')
        if isinstance(shown, list):
            return [str(entry) for entry in shown]
        if shown:                       # a single id from an earlier format
            return [str(shown)]
        return []
    except Exception:
        LOG.exc('could not read %s - treating everything as unseen' % path)
        return []


def _write_seen(ids):
    path = _seen_file()
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            json.dump({'shown': list(ids)}, handle, indent=4)
    except Exception:
        LOG.exc('could not write %s - the notes may be shown again' % path)


def _read_notes():
    """patchnotes.json, or None. Same VFS read as i18n.py - a .wotmod's
    contents are not a real filesystem path, so open() cannot reach them."""
    try:
        section = ResMgr.openSection(_NOTES_PATH)
        if section is None or not ResMgr.isFile(_NOTES_PATH):
            LOG.info('no patchnotes.json in this build')
            return None
        data = json.loads(section.asBinary)
        entries = data.get('notes')
        if not isinstance(entries, list):
            LOG.warning('patchnotes.json: "notes" must be a list of entries')
            return None
        return entries
    except Exception:
        LOG.exc('failed to read %s' % _NOTES_PATH)
        return None


def _lines_of(entry):
    """An entry's lines in the client's language, falling back to English.

    Unlike i18n.t(), a missing language here is normal and must not be loud:
    release notes change every time, and translating them into all 28 languages
    per release is not realistic. English is a useful fallback; !!key!! is not.
    """
    lines = entry.get(i18n.language()) or entry.get(_FALLBACK_LANGUAGE) or []
    return [line for line in lines if line]


def _format(entries):
    """The message body: a styled heading, then one bulleted line per note.

    Plain text came out as one run-on paragraph. <br/> is honoured in service
    channel messages (the client's own item lists use it), but line breaks
    alone give no structure - the bullets and the heading style are what make
    it readable. Several versions at once each get their own sub-heading, so it
    stays obvious which release a line belongs to.
    """
    blocks = [text_styles.middleTitle(t('patchNotesHeader'))]
    multiple = len(entries) > 1
    for entry in entries:
        lines = _lines_of(entry)
        if not lines:
            continue
        if multiple:
            blocks.append(text_styles.goldColor(entry.get('version') or u''))
        for line in lines:
            blocks.append(text_styles.main(_BULLET + line))
    return u'<br/>'.join(blocks) if len(blocks) > 1 else u''


def maybe_show():
    """Show every entry the player has not seen yet, then record them all.

    Safe to call on every hangar load: the ids are written in the same step
    that shows them, and a fresh install only records without showing.
    """
    try:
        entries = _read_notes()
        if not entries:
            return
        ids = [str(entry.get('version')) for entry in entries if entry.get('version')]
        if not ids:
            LOG.warning('patchnotes.json: no entry carries a version')
            return

        seen = _read_seen()
        unseen = [entry for entry in entries
                  if str(entry.get('version')) not in seen]
        if not unseen:
            return

        if config.was_fresh_install():
            # Nothing was replaced, so there is nothing to report: a changelog
            # for a player who never had the previous version is just noise.
            # Recording still matters, or the next start would show it.
            _write_seen(ids)
            LOG.info('fresh install - recorded %d note id(s) without showing' % len(ids))
            return

        body = _format(unseen)
        if body:
            # HIGH so the notification is actually surfaced rather than only
            # filed in the centre: the notes are shown once per release, so a
            # player who misses the pop-up never sees them again.
            messages.push_info(body, priority=NotificationPriorityLevel.HIGH)
            LOG.info('showed patch notes for %s'
                     % ', '.join(str(e.get('version')) for e in unseen))
        else:
            LOG.warning('no text for %s - recording anyway so it cannot surface later'
                        % ', '.join(str(e.get('version')) for e in unseen))
        # Every id is recorded, not just the ones shown: an entry without text
        # must not re-check on every hangar load for the life of the build.
        _write_seen(ids)
    except Exception:
        LOG.exc('maybe_show failed')
