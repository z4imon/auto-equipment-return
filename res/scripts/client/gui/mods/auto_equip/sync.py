# -*- coding: utf-8 -*-
"""Cloud equipment sync: pairs with the z4imon.de server through a
WG-account-verified device-pairing flow (see /link + /auth/callback on the
server), then keeps saved sets in sync across a player's own PCs.

    sync.transport   the urllib2 wrapper below - runs off the main thread
    sync.pairing     start_pairing/poll/disconnect (Task 10)
    sync.reconcile   full_reconcile + per-vehicle debounced push (Task 11)
    sync.panel       the ModsSettingsAPI checkbox (Task 12)

All of it lives in one file on purpose, same as every other single-purpose
module in this package - see mod_auto_equip.py's module list.
"""

import json
import os
import threading
import time

try:
    import urllib2
except ImportError:
    urllib2 = None  # never true on the shipped client; guards local imports

import BigWorld

from . import config
from .log import LOG

SERVER_BASE_URL = 'https://z4imon.de/api/auto-equipment-return'
REQUEST_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Local pairing file - <preferences>/mods/z4imon/autoequipmentreturn/<accountId>.sync.json
# ---------------------------------------------------------------------------

def _pairing_path(account_id):
    return os.path.join(config.account_files_dir(), '%s.sync.json' % account_id)


def _read_pairing(account_id):
    path = _pairing_path(account_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as handle:
            return json.load(handle)
    except Exception:
        LOG.exc('sync: failed reading pairing file for %s' % account_id)
        return None


def _write_pairing(account_id, data):
    path = _pairing_path(account_id)
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        json.dump(data, handle)


def _forget_pairing(account_id):
    path = _pairing_path(account_id)
    if os.path.exists(path):
        os.remove(path)


def is_paired(account_id):
    return _read_pairing(account_id) is not None


def current_token(account_id):
    pairing = _read_pairing(account_id)
    return pairing['accessToken'] if pairing else None


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def _request(method, path, token=None, body=None):
    """Blocking HTTP call - MUST run off the main thread, see call_async()."""
    url = SERVER_BASE_URL + path
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer %s' % token
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib2.Request(url, data=data, headers=headers)
    request.get_method = lambda: method
    try:
        response = urllib2.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)
        raw = response.read()
        return response.getcode(), (json.loads(raw) if raw else None)
    except urllib2.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


def call_async(method, path, token=None, body=None, callback=None):
    """Runs _request() on a background thread, delivers (status, data) back
    on the main thread via BigWorld.callback - never touch game state from
    the request thread itself."""
    def worker():
        try:
            status, data = _request(method, path, token=token, body=body)
        except Exception:
            LOG.exc('sync: request failed: %s %s' % (method, path))
            status, data = None, None
        if callback is not None:
            BigWorld.callback(0, lambda: callback(status, data))

    threading.Thread(target=worker).start()
