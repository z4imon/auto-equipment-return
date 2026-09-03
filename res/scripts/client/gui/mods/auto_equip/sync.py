# -*- coding: utf-8 -*-
"""Cloud equipment sync: pairs with the z4imon.de server through a
WG-account-verified device-pairing flow (see /link + /auth/callback on the
server), then keeps saved sets in sync across a player's own PCs.

    sync.transport   the urllib2 wrapper below - runs off the main thread
    sync.pairing     start_pairing/poll/disconnect (Task 10)
    sync.reconcile   full_reconcile + per-vehicle debounced push (Task 11)
    sync.panel       the ModsListAPI toggle button

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
from . import messages
from .i18n import t
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
    return pairing.get('accessToken') if pairing else None


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

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()


# ---------------------------------------------------------------------------
# Device-pairing flow
# ---------------------------------------------------------------------------

_POLL_INTERVAL_DEFAULT = 5
# How long a run of back-to-back connection failures (status is None - the
# request never even reached the server) may continue before giving up.
# Deliberately NOT a total-pairing-duration timeout: a live server that
# keeps answering "still pending" is left alone indefinitely, since the
# device code's own server-side expiry (-> 404 -> syncPairingExpired)
# already covers a player who's just slow to finish the browser step.
_POLL_FAILURE_TIMEOUT_SECONDS = 120


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
        messages.push_info(t('syncPairingStarted'))
        _poll_pairing(account_id, data['deviceCode'], data.get('intervalSeconds', _POLL_INTERVAL_DEFAULT))

    call_async('POST', '/auth/device', body=body, callback=handle_device_response)


def _poll_pairing(account_id, device_code, interval_seconds):
    # Mutable single-element list, not a plain variable - handle_poll_response
    # needs to write to it across calls, and Python 2 closures can't rebind an
    # enclosing-scope name (no nonlocal).
    failing_since = [None]

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
            if not data.get('accessToken') or not data.get('realm'):
                LOG.warning('sync: authorized poll response missing accessToken/realm, discarding')
                messages.push_error(t('syncPairingMismatch'))
                return
            _write_pairing(account_id, {
                'accessToken': data.get('accessToken'),
                'realm': data.get('realm'),
                'pairedAt': time.time(),
            })
            messages.push_info(t('syncPairingDone'))
            full_reconcile(account_id)
            _update_modlist_button(account_id)
            _update_sharing_button(account_id)
            return
        if status is None:
            # The request never reached the server at all (offline/DNS/
            # timeout) - a real "still pending" response never lands here,
            # so this can't cut off a slow-but-connected player.
            if failing_since[0] is None:
                failing_since[0] = time.time()
            elif time.time() - failing_since[0] >= _POLL_FAILURE_TIMEOUT_SECONDS:
                LOG.warning('sync: pairing poll gave up after %ss of connection failures (account=%s)'
                            % (_POLL_FAILURE_TIMEOUT_SECONDS, account_id))
                messages.push_error(t('syncPairingFailed'))
                return
        else:
            failing_since[0] = None  # the server answered - connection is fine
        # still pending - poll again after the server-suggested interval
        BigWorld.callback(interval_seconds, lambda: call_async(
            'POST', '/auth/token', body={'deviceCode': device_code}, callback=handle_poll_response))

    call_async('POST', '/auth/token', body={'deviceCode': device_code}, callback=handle_poll_response)


def disconnect(account_id):
    """Revokes this PC's token server-side, deletes ALL of this account's
    saved equipment on the server (not just this device's data - the player
    is warned about this in the settings-panel copy before triggering it),
    and forgets the local pairing. The server delete must be attempted while
    the token is still valid, so it happens before the token is revoked.

    If the server-side delete fails (offline, 5xx, ...), everything stops
    right there: the token stays valid and the local pairing file stays put,
    so the player is still "connected" and can just try again. Revoking the
    token or forgetting the pairing anyway would stop the sets from ever
    actually being deleted (the token was the only credential that could
    retry it) while showing the player a clean "disconnected" state - the
    data would sit there orphaned server-side and quietly resurrect itself
    the next time this account paired anywhere."""
    token = current_token(account_id)
    if token is None:
        return

    def after_data_deleted(status, data):
        if status != 200:
            LOG.warning('sync: account data delete failed (status=%s), aborting disconnect' % status)
            messages.push_error(t('syncDisconnectFailed'))
            return
        call_async('DELETE', '/auth/token', token=token, callback=lambda s, d: None)
        _forget_pairing(account_id)
        _update_modlist_button(account_id)
        candidacy = _streamer_candidacy.get(account_id)
        if candidacy is not None:
            candidacy['activated'] = False
        _update_sharing_button(account_id)

    call_async('DELETE', '/accounts/%s' % account_id, token=token, callback=after_data_deleted)


def check_cloud_data(account_id):
    """Called at login only when NOT already paired - lets a fresh, unpaired
    PC discover that cloud-saved equipment exists for this account, without
    needing a token (the has-data endpoint is intentionally unauthenticated -
    it only ever reveals a yes/no signal, never actual equipment content)."""
    def handle_response(status, data):
        if status == 200 and data and data.get('hasData'):
            messages.push_info(t('syncCloudDataAvailableNotification'))
    call_async('GET', '/accounts/%s/has-data' % account_id, callback=handle_response)


# ---------------------------------------------------------------------------
# Bidirectional reconcile - called at login and right after a fresh pairing
# ---------------------------------------------------------------------------

def _handle_auth_failure(account_id, status):
    """A 401 means the token is gone (expired or revoked) - there is nothing
    to retry, so forget the pairing now rather than silently failing sync
    forever with a button that still reads "connected"."""
    if status == 401:
        _forget_pairing(account_id)
        messages.push_warning(t('syncPairingExpired'))
        return True
    return False


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
            if _handle_auth_failure(account_id, status):
                return
            LOG.warning('sync: full pull failed (status=%s)' % status)
            return
        sets = data.get('sets', {})
        if not isinstance(sets, dict):
            LOG.warning('sync: full pull returned malformed sets (type=%s)' % type(sets))
            sets = {}
        to_push = _merge_server_sets(sets)
        for veh_inv_id in to_push:
            _sync_vehicle(account_id, veh_inv_id)

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
            deleted = bool(server_entry.get('deleted'))
            config.apply_remote_entry(inv_id, server_entry['set1'], server_entry['set2'],
                                      server_entry.get('vehicleCD'),
                                      server_entry['updatedAt'], notify=False, deleted=deleted)
            to_push.discard(inv_id)
        except Exception:
            LOG.exc('sync: failed merging server entry for %s' % (inv_id,))
    return list(to_push)


# ---------------------------------------------------------------------------
# Push on local change - debounced per vehicle
# ---------------------------------------------------------------------------

_pending_pushes = {}  # invID -> BigWorld callback id
_PUSH_DEBOUNCE_SECONDS = 3

# invIDs with a push/delete request currently in flight, and invIDs that need
# another sync once their in-flight one completes - see _sync_vehicle().
_in_flight_syncs = set()
_pending_resyncs = set()


def _on_local_change(veh_inv_id, entry):
    _schedule_push(veh_inv_id)


def _schedule_push(veh_inv_id):
    existing = _pending_pushes.pop(veh_inv_id, None)
    if existing is not None:
        BigWorld.cancelCallback(existing)

    def fire():
        _pending_pushes.pop(veh_inv_id, None)
        account_id = config.current_account_id()
        if account_id and is_paired(account_id):
            _sync_vehicle(account_id, veh_inv_id)

    _pending_pushes[veh_inv_id] = BigWorld.callback(_PUSH_DEBOUNCE_SECONDS, fire)


def _sync_vehicle(account_id, veh_inv_id):
    """Pushes or deletes one vehicle's saved sets - whichever the freshest
    local state calls for - and is the single entry point both
    full_reconcile's post-merge push and the debounced on-change push go
    through, so at most one request for a given vehicle is ever in flight.

    Without this, those two callers could each have a PUT/DELETE for the
    same vehicle in flight at once: full_reconcile's push fires the moment
    its pull response lands, independently of the debounce timer a local
    edit already started. The server stamps updatedAt with ITS OWN clock at
    request-processing time (equipment_store.py's put_vehicle/
    delete_vehicle), not from anything the client sends - so whichever of
    the two requests the server happens to process LAST wins, regardless of
    which one actually reflects the more recent local edit. A call made
    while one is already in flight is deferred instead, and re-fires - again
    reading whatever is freshest in local config - once the first
    completes."""
    if veh_inv_id in _in_flight_syncs:
        _pending_resyncs.add(veh_inv_id)
        return
    _in_flight_syncs.add(veh_inv_id)
    entry = config.saved_sets(veh_inv_id)
    if entry is not None and entry.get('deleted'):
        _delete_vehicle_remote(account_id, veh_inv_id, _on_sync_done)
    else:
        _push_vehicle(account_id, veh_inv_id, _on_sync_done)


def _on_sync_done(account_id, veh_inv_id):
    _in_flight_syncs.discard(veh_inv_id)
    if veh_inv_id in _pending_resyncs:
        _pending_resyncs.discard(veh_inv_id)
        _sync_vehicle(account_id, veh_inv_id)


def _push_vehicle(account_id, veh_inv_id, on_done=None):
    token = current_token(account_id)
    entry = config.saved_sets(veh_inv_id)
    if token is None or entry is None:
        if on_done:
            on_done(account_id, veh_inv_id)
        return
    body = {'set1': entry['set1'], 'set2': entry['set2'], 'vehicleCD': entry['vehicleCD']}

    def handle_push(status, data):
        try:
            if status == 200 and data:
                config.set_updated_at(veh_inv_id, data['updatedAt'])
            elif not _handle_auth_failure(account_id, status):
                LOG.warning('sync: push of %s failed (status=%s)' % (veh_inv_id, status))
        finally:
            if on_done:
                on_done(account_id, veh_inv_id)

    call_async('PUT', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id), token=token,
              body=body, callback=handle_push)


def _delete_vehicle_remote(account_id, veh_inv_id, on_done=None):
    token = current_token(account_id)
    if token is None:
        if on_done:
            on_done(account_id, veh_inv_id)
        return

    def handle_delete(status, data):
        try:
            if status != 200 and not _handle_auth_failure(account_id, status):
                LOG.warning('sync: delete of %s failed (status=%s)' % (veh_inv_id, status))
        finally:
            if on_done:
                on_done(account_id, veh_inv_id)

    call_async('DELETE', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id), token=token,
              callback=handle_delete)


# ---------------------------------------------------------------------------
# ModsListAPI button - a single button toggling sync on/off, swapping its own
# label/description in place to show the current state
# ---------------------------------------------------------------------------

_MODLIST_ID = 'z4imon.auto_equipment_return.sync'

# Same two icons for both modlist buttons in this file - green while the
# toggle they represent is ON, orange while it's OFF (independent of whether
# the button itself is currently clickable - see _update_sharing_button,
# whose "off" icon shows even while greyed out for "cloud sync isn't on
# yet"). Paths are resource-VFS-relative, as g_modsListApi.addModification's
# icon param requires (validated via ResMgr.isFile against this exact
# string, before the API's own '../../' prefix gets added internally).
_ICON_ENABLED = 'gui/maps/icons/z4imon/GreenGlow.png'
_ICON_DISABLED = 'gui/maps/icons/z4imon/OrangeGlow.png'


def _state_icon(enabled):
    return _ICON_ENABLED if enabled else _ICON_DISABLED


def _aslain_mod_menu_installed():
    """True only when Aslain's Mod Menu itself answers to gui.aslainMenu -
    gates the {ATTENTION}-formatted disconnect warning below. Aslain
    confirmed their renderer supports {HEADER}/{BODY}/{ATTENTION} markup,
    but gui.modsListApi may just as easily be answered by something else
    (the native mods list, or another package) that does not - sending the
    markup there would show the raw {ATTENTION} tags instead of a
    formatted warning band."""
    try:
        import gui.aslainMenu  # noqa: F401
        return True
    except ImportError:
        return False


def _modlist_button_text(enabled):
    if enabled:
        tooltip = (t('syncModListDisableTooltipAslain') if _aslain_mod_menu_installed()
                   else t('syncModListDisableTooltip'))
        return t('syncModListDisableLabel'), tooltip
    return t('syncCheckboxLabel'), t('syncCheckboxTooltip')


def _update_modlist_button(account_id):
    try:
        from gui.modsListApi import g_modsListApi
    except Exception:
        return
    paired = is_paired(account_id)
    name, description = _modlist_button_text(paired)
    g_modsListApi.addModification(
        id=_MODLIST_ID, name=name, description=description, icon=_state_icon(paired),
        enabled=True, login=False, lobby=True,
        callback=lambda: _on_modlist_click(account_id),
    )


def _on_modlist_click(account_id):
    if is_paired(account_id):
        disconnect(account_id)
    else:
        start_pairing(account_id)


# ---------------------------------------------------------------------------
# Streamer sharing self-service - a second ModsListAPI button, visible only
# to accounts the server has flagged as candidates for the streamer picker
# ---------------------------------------------------------------------------

_MODLIST_SHARING_ID = 'z4imon.auto_equipment_return.sharing'

_streamer_candidacy = {}  # account_id -> {'activated': bool}, only present once known-candidate


def check_streamer_candidate(account_id, callback):
    """callback(is_candidate, activated). Unauthenticated GET, called at
    every register() regardless of pairing state - mirrors check_cloud_data's
    login-time pattern, but runs unconditionally rather than only-when-
    unpaired, since a candidate might already be paired."""
    def handle_response(status, data):
        if status == 200 and data:
            callback(bool(data.get('isCandidate')), bool(data.get('activated')))
        else:
            callback(False, False)
    call_async('GET', '/streamers/%s/is-candidate' % account_id, callback=handle_response)


def set_streamer_sharing(account_id, activated, callback=None):
    """Flips this account's own sharing consent via the self-service
    endpoint - only works while paired, since the endpoint requires a
    bearer token bound to this exact account_id."""
    token = current_token(account_id)
    if token is None:
        if callback is not None:
            callback(False)
        return

    def handle_response(status, data):
        if callback is not None:
            callback(status == 200 and bool(data and data.get('activated')) == activated)

    call_async('PUT', '/streamers/%s/sharing' % account_id, token=token,
              body={'activated': activated}, callback=handle_response)


def _on_candidacy_known(account_id, is_candidate, activated):
    if not is_candidate:
        return
    _streamer_candidacy[account_id] = {'activated': activated}
    _update_sharing_button(account_id)


def _sharing_modlist_button_text(paired, activated):
    if not paired:
        return t('sharingModListDisabledLabel'), t('sharingModListDisabledTooltip')
    if activated:
        return t('sharingModListDisableLabel'), t('sharingModListDisableTooltip')
    return t('sharingModListLabel'), t('sharingModListTooltip')


def _update_sharing_button(account_id):
    candidacy = _streamer_candidacy.get(account_id)
    if candidacy is None:
        return
    try:
        from gui.modsListApi import g_modsListApi
    except Exception:
        return
    paired = is_paired(account_id)
    activated = candidacy.get('activated', False)
    name, description = _sharing_modlist_button_text(paired, activated)
    g_modsListApi.addModification(
        id=_MODLIST_SHARING_ID, name=name, description=description, icon=_state_icon(activated),
        enabled=paired, login=False, lobby=True,
        callback=lambda: _on_sharing_modlist_click(account_id),
    )


def _on_sharing_modlist_click(account_id):
    if not is_paired(account_id):
        return
    candidacy = _streamer_candidacy.get(account_id)
    if candidacy is None:
        return
    new_state = not candidacy.get('activated', False)

    def handle_result(ok):
        if ok:
            candidacy['activated'] = new_state
        else:
            messages.push_error(t('sharingToggleFailed'))
        _update_sharing_button(account_id)

    set_streamer_sharing(account_id, new_state, callback=handle_result)


def register(account_id):
    """Registers/refreshes the ModsListAPI button. No-op when the API isn't
    installed - same degrade-quietly convention as the rest of this mod."""
    _update_modlist_button(account_id)
    check_streamer_candidate(account_id, callback=lambda is_candidate, activated:
                              _on_candidacy_known(account_id, is_candidate, activated))
    LOG.info('sync: registered ModsListAPI button (enabled=%s)' % is_paired(account_id))
