# -*- coding: utf-8 -*-
"""When each vehicle was last *used*, so inventory.py's donor search can take a
device from the vehicle that has sat untouched the longest instead of from
whichever one the garage happened to hand it first.

    <preferences>/mods/z4imon/autoequipmentreturn/<accountId>.usage.json
    {"version": 1, "vehicles": {"<vehicle invID>": <unix timestamp>}}

Why this exists
---------------
Equipment is one physical device per copy. A player who owns three "Improved
Exhaust" and has them mounted on three tanks still cannot serve a fourth tank
without demounting one of the three. The donor search already ranks candidates
by INTENT (does this vehicle even want the device back?) and by COST (can it be
taken without a setup switch?), but it had no notion of TIME - so between two
candidates that were equal on both, the winner was simply the first one the
items cache happened to yield.

With a stable cache order that is not random, it is a fixed vehicle - and
watching a player bounce between two tanks makes it obvious which one: the tank
they just left. A -> B pulls the device off A, B -> A pulls it straight back off
B, and both tanks are re-equipped on every single selection while a third tank
sitting on an unused copy is never asked. Nothing in that loop is wrong per se;
it is just permanently worst-case.

Ranking a candidate that was used more recently as *less* attractive makes the
same choice fall on the copy that has been gathering dust, which is the LRU
idea and the reason the loop above collapses: after the first visit B keeps its
device, so coming back to A finds its set already installed and the run ends
before it sends a single command (apply._plan_already_applied).

What counts as "used"
---------------------
Being selected in the garage, nothing else. It is the only signal the client
gives for "the player is working with this tank", and it is exactly what makes
A the worst possible donor while the player is on B. A successful apply does
NOT touch this on its own: batch runs install across the whole garage and
would otherwise stamp every Primary vehicle with the same instant, which would
make the whole ordering meaningless.

Hovering, previewing and inspecting do not count either - they are reads, and
the popover pushes them at the client constantly.

Storage
-------
A file of its own rather than a key in <accountId>.settings.json: this changes
on every vehicle selection (hundreds per session), while the settings
file is a hand-editable document that should not grow a several-hundred-entry
dict that changes under the player's feet. Same reasoning mode_sets.py uses for
its own per-profile files.

The account importer only ever lists <digits>.json, so this file never shows up
as an importable account.

Changes are written a few seconds after the last selection (_FLUSH_DELAY_SECONDS),
so quickly clicking through the carousel costs one write instead of one per
tank. Account switches and game shutdown write out whatever is still pending.

Losing this file is harmless: every vehicle reads back as "never used", which
restores the old first-match behaviour exactly.
"""

import json
import os
import time

import BigWorld

from . import config
from .log import LOG

_FILE_VERSION = 1

# Repeated touches of the SAME vehicle within this window are letterboxed into
# one. g_currentVehicle.onChanged fires in storms (every items-cache resync
# does it, see apply.py's _last_inv_id), and without this a single selection
# would rewrite the file dozens of times.
_REPEAT_WINDOW_SECONDS = 2.0

# The garage of a well-played account can hold several hundred vehicles; this
# only has to stay comfortably above that while keeping the file small.
_MAX_ENTRIES = 1000

# How long after the last touch the file is written. Several selections inside
# this window end up in one write.
_FLUSH_DELAY_SECONDS = 5.0

_account_id = 0
_last_used = {}         # str(vehicle invID) -> float, unix time of last selection
_loaded = False
_most_recent = None     # invID of the last touch, for _REPEAT_WINDOW_SECONDS
_dirty = False          # changes not yet written to disk
_flush_callback = None  # BigWorld callback id of the pending write, or None


# ---------------------------------------------------------------------------
# Loading and saving
# ---------------------------------------------------------------------------

def _path(account_id):
    return os.path.join(config.account_files_dir(), '%s.usage.json' % account_id)


def load_for_account(account_id):
    """(Re)reads the usage file for `account_id`. Called from the same account
    load sequence as config.load_for_account - the donor search reads this
    during a run, which can only happen after an account is known."""
    global _account_id, _last_used, _loaded, _most_recent
    flush()             # the previous account's pending changes, if any
    _account_id = account_id
    _most_recent = None
    _loaded = True
    _last_used = {}

    path = _path(account_id)
    if not os.path.exists(path):
        return          # first run for this account: nothing invented, nothing written
    try:
        with open(path, 'r') as handle:
            data = json.load(handle)
        raw = data.get('vehicles') if isinstance(data, dict) else None
        if isinstance(raw, dict):
            for key, value in raw.iteritems():
                try:
                    _last_used[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        LOG.info('usage: loaded %d vehicle timestamp(s) for account %s'
                 % (len(_last_used), account_id))
    except Exception:
        LOG.exc('usage: could not read %s' % path)
        # Keep the unreadable file rather than overwrite it on the next touch -
        # same courtesy config.py extends to a broken settings file.
        try:
            broken = path + '.broken'
            if os.path.exists(broken):
                os.remove(broken)
            os.rename(path, broken)
        except Exception:
            LOG.exc('usage: could not set %s aside' % path)
        _last_used = {}


def flush():
    """Writes pending changes out now and cancels the scheduled write. Does
    nothing when there is nothing new to write."""
    global _dirty
    _cancel_scheduled_flush()
    if not _dirty or not _loaded or not _account_id:
        return False
    _dirty = False
    path = _path(_account_id)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
        payload = {'version': _FILE_VERSION,
                   'vehicles': dict((key, value) for key, value in _last_used.iteritems())}
        config.write_json_atomic(path, payload)
        return True
    except Exception:
        LOG.exc('usage: could not write %s' % path)
        return False


def _schedule_flush():
    global _flush_callback
    if _flush_callback is None:
        _flush_callback = BigWorld.callback(_FLUSH_DELAY_SECONDS, _on_flush_timer)


def _cancel_scheduled_flush():
    global _flush_callback
    if _flush_callback is not None:
        try:
            BigWorld.cancelCallback(_flush_callback)
        except Exception:
            pass        # already fired or invalid: nothing left to cancel
        _flush_callback = None


def _on_flush_timer():
    global _flush_callback
    _flush_callback = None
    try:
        flush()
    except Exception:
        LOG.exc('usage: scheduled write failed')


# ---------------------------------------------------------------------------
# Recording and reading
# ---------------------------------------------------------------------------

def touch(veh_inv_id):
    """Records that `veh_inv_id` is the vehicle the player is using right now.
    Returns True when the stored value actually changed."""
    global _most_recent, _dirty
    if not _loaded or not _account_id or not veh_inv_id:
        return False
    key = str(veh_inv_id)
    now = time.time()

    if _most_recent == key:
        previous = _last_used.get(key)
        if previous is not None and 0 <= now - previous < _REPEAT_WINDOW_SECONDS:
            return False        # the same selection reported again, not a new one

    _most_recent = key
    _last_used[key] = now
    _prune()
    _dirty = True
    _schedule_flush()
    return True


def last_used(veh_inv_id):
    """The unix time this vehicle was last selected, or 0.0 when it never was.

    0.0 is deliberately the "oldest possible" answer: a vehicle this account
    has never selected is the best possible donor as far as recency goes, which
    is what the pre-LRU behaviour effectively assumed for everything."""
    return _last_used.get(str(veh_inv_id), 0.0)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def _prune():
    """Drops the oldest entries once the map outgrows _MAX_ENTRIES. Only a
    garage larger than that threshold is ever affected, and only its least
    recently used vehicles - which are the ones a donor search would want
    most, so they are also the ones whose exact timestamp matters least."""
    excess = len(_last_used) - _MAX_ENTRIES
    if excess <= 0:
        return
    oldest = sorted(_last_used.items(), key=lambda pair: pair[1])[:excess]
    for key, _value in oldest:
        _last_used.pop(key, None)
