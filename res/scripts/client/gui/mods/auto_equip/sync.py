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


# ---------------------------------------------------------------------------
# Device-pairing flow
# ---------------------------------------------------------------------------

from . import messages
from .i18n import t

_POLL_INTERVAL_DEFAULT = 5


def _current_realm():
    try:
        from constants import AUTH_REALM
    except Exception:
        AUTH_REALM = 'EU'
    return unicode(AUTH_REALM or 'EU').upper()


def start_pairing(account_id):
    """Kicks off the device-pairing flow: asks the server for a device/user
    code pair, opens the WG login page in the system browser, then polls
    until the server confirms it (or it expires)."""
    body = {'accountId': account_id, 'realm': _current_realm()}

    def handle_device_response(status, data):
        if status != 200 or data is None:
            LOG.warning('sync: /auth/device failed (status=%s)' % status)
            messages.push_error(t('syncPairingFailed'))
            return
        BigWorld.wg_openWebBrowser(str(data['verificationUri']))
        messages.push_info(t('syncPairingStarted', userCode=data['userCode']))
        _poll_pairing(account_id, data['deviceCode'], data.get('intervalSeconds', _POLL_INTERVAL_DEFAULT))

    call_async('POST', '/auth/device', body=body, callback=handle_device_response)


def _poll_pairing(account_id, device_code, interval_seconds):
    def handle_poll_response(status, data):
        if status == 404:
            messages.push_error(t('syncPairingExpired'))
            return
        if status == 403:
            messages.push_error(t('syncPairingMismatch'))
            return
        if status == 200 and data and data.get('status') == 'authorized':
            if data.get('accountId') != account_id:
                LOG.warning('sync: paired token accountId mismatch, discarding')
                messages.push_error(t('syncPairingMismatch'))
                return
            _write_pairing(account_id, {
                'accessToken': data['accessToken'],
                'realm': data['realm'],
                'pairedAt': time.time(),
            })
            messages.push_info(t('syncPairingDone'))
            full_reconcile(account_id)
            return
        # still pending - poll again after the server-suggested interval
        BigWorld.callback(interval_seconds, lambda: call_async(
            'POST', '/auth/token', body={'deviceCode': device_code}, callback=handle_poll_response))

    call_async('POST', '/auth/token', body={'deviceCode': device_code}, callback=handle_poll_response)


def disconnect(account_id):
    """Revokes this PC's token server-side and forgets it locally. Other
    paired PCs for the same account are untouched - each has its own token
    (see the server's token_store, keyed by token, not by account)."""
    token = current_token(account_id)
    if token is None:
        return
    call_async('DELETE', '/auth/token', token=token, callback=lambda status, data: None)
    _forget_pairing(account_id)


# ---------------------------------------------------------------------------
# Bidirectional reconcile - called at login and right after a fresh pairing
# ---------------------------------------------------------------------------

def full_reconcile(account_id):
    """Pulls the account's server-stored sets, merges them into the local
    config (newest updatedAt per vehicle wins), then pushes every local
    entry that the server is missing or that just won the merge. This is
    the one function that makes a freshly-paired, empty-local-config PC
    catch up immediately instead of waiting for the next login."""
    token = current_token(account_id)
    if token is None:
        return

    def handle_pull(status, data):
        if status != 200 or data is None:
            LOG.warning('sync: full pull failed (status=%s)' % status)
            return
        sets = data.get('sets', {})
        if not isinstance(sets, dict):
            LOG.warning('sync: full pull returned malformed sets (type=%s)' % type(sets))
            sets = {}
        to_push = _merge_server_sets(sets)
        for veh_inv_id in to_push:
            _push_vehicle(account_id, veh_inv_id)

    call_async('GET', '/accounts/%s' % account_id, token=token, callback=handle_pull)


def _merge_server_sets(server_sets):
    """Applies the server's view onto the local config in-place. Returns the
    invIDs that still need pushing afterwards (local-only, or local won)."""
    to_push = set(config.all_saved_inv_ids())
    for inv_id, server_entry in server_sets.items():
        try:
            local_entry = config.saved_sets(inv_id)
            local_updated = local_entry['updatedAt'] if local_entry else None
            if local_updated is not None and local_updated >= server_entry['updatedAt']:
                continue  # local wins or ties - stays in to_push, gets pushed below
            if server_entry.get('deleted'):
                config.delete_sets(inv_id, notify=False)
            else:
                config.store_sets(inv_id, set1=server_entry['set1'], set2=server_entry['set2'],
                                  veh_cd=server_entry.get('vehicleCD'),
                                  updated_at=server_entry['updatedAt'], notify=False)
            to_push.discard(inv_id)
        except Exception:
            LOG.exc('sync: failed merging server entry for %s' % (inv_id,))
    return list(to_push)


# ---------------------------------------------------------------------------
# Push on local change - debounced per vehicle
# ---------------------------------------------------------------------------

_pending_pushes = {}  # invID -> BigWorld callback id
_PUSH_DEBOUNCE_SECONDS = 3


def _on_local_change(veh_inv_id, entry):
    _schedule_push(veh_inv_id, delete=(entry is None))


def _schedule_push(veh_inv_id, delete):
    existing = _pending_pushes.pop(veh_inv_id, None)
    if existing is not None:
        BigWorld.cancelCallback(existing)

    def fire():
        _pending_pushes.pop(veh_inv_id, None)
        account_id = config.current_account_id()
        if account_id and is_paired(account_id):
            if delete:
                _delete_vehicle_remote(account_id, veh_inv_id)
            else:
                _push_vehicle(account_id, veh_inv_id)

    _pending_pushes[veh_inv_id] = BigWorld.callback(_PUSH_DEBOUNCE_SECONDS, fire)


def _push_vehicle(account_id, veh_inv_id):
    token = current_token(account_id)
    entry = config.saved_sets(veh_inv_id)
    if token is None or entry is None:
        return
    body = {'set1': entry['set1'], 'set2': entry['set2'], 'vehicleCD': entry['vehicleCD']}

    def handle_push(status, data):
        if status == 200 and data:
            config.set_updated_at(veh_inv_id, data['updatedAt'])
        else:
            LOG.warning('sync: push of %s failed (status=%s)' % (veh_inv_id, status))

    call_async('PUT', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id), token=token,
              body=body, callback=handle_push)


def _delete_vehicle_remote(account_id, veh_inv_id):
    token = current_token(account_id)
    if token is None:
        return
    call_async('DELETE', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id), token=token,
              callback=lambda status, data: None)
