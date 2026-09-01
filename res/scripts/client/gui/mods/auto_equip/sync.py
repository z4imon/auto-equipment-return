# -*- coding: utf-8 -*-
"""Cloud equipment sync: keeps saved sets in sync across a player's own PCs
via the z4imon.de server.

    sync.transport   the urllib2 wrapper below - runs off the main thread
    sync.reconcile   full_reconcile + per-vehicle debounced push (Task 11)
    sync.panel       the ModsSettingsAPI checkbox (Task 12)

There is no authentication on this path right now - the server trusts
`account_id` directly. This is a temporary state for functional testing; a
real auth scheme replaces it later (see config.is_sync_enabled/
set_sync_enabled for the local on/off flag that stands in for pairing in the
meantime).

All of it lives in one file on purpose, same as every other single-purpose
module in this package - see mod_auto_equip.py's module list.
"""

import json
import threading

try:
    import urllib2
except ImportError:
    urllib2 = None  # never true on the shipped client; guards local imports

import BigWorld

from . import config
from .i18n import t
from .log import LOG

SERVER_BASE_URL = 'https://z4imon.de/api/auto-equipment-return'
REQUEST_TIMEOUT_SECONDS = 10


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
# Bidirectional reconcile - called at login and whenever sync is turned on
# ---------------------------------------------------------------------------

def full_reconcile(account_id):
    """Pulls the account's server-stored sets, merges them into the local
    config (newest updatedAt per vehicle wins), then pushes every local
    entry that the server is missing or that just won the merge. This is
    the one function that makes a freshly-enabled, empty-local-config PC
    catch up immediately instead of waiting for the next login."""

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

    call_async('GET', '/accounts/%s' % account_id, callback=handle_pull)


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
                config.apply_remote_entry(inv_id, server_entry['set1'], server_entry['set2'],
                                          server_entry.get('vehicleCD'),
                                          server_entry['updatedAt'], notify=False)
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
        if account_id and config.is_sync_enabled():
            if delete:
                _delete_vehicle_remote(account_id, veh_inv_id)
            else:
                _push_vehicle(account_id, veh_inv_id)

    _pending_pushes[veh_inv_id] = BigWorld.callback(_PUSH_DEBOUNCE_SECONDS, fire)


def _push_vehicle(account_id, veh_inv_id):
    entry = config.saved_sets(veh_inv_id)
    if entry is None:
        return
    body = {'set1': entry['set1'], 'set2': entry['set2'], 'vehicleCD': entry['vehicleCD']}

    def handle_push(status, data):
        if status == 200 and data:
            config.set_updated_at(veh_inv_id, data['updatedAt'])
        else:
            LOG.warning('sync: push of %s failed (status=%s)' % (veh_inv_id, status))

    call_async('PUT', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id),
              body=body, callback=handle_push)


def _delete_vehicle_remote(account_id, veh_inv_id):
    def handle_delete(status, data):
        if status != 200:
            LOG.warning('sync: delete of %s failed (status=%s)' % (veh_inv_id, status))

    call_async('DELETE', '/accounts/%s/vehicles/%s' % (account_id, veh_inv_id),
              callback=handle_delete)


# ---------------------------------------------------------------------------
# ModsSettingsAPI panel - a single checkbox toggling sync on/off
# ---------------------------------------------------------------------------

_MOD_LINKAGE = 'z4imon.auto_equipment_return.sync'
_VAR_SYNC_ACTIVE = 'syncActive'

_account_id = None


def _build_template(account_id, templates):
    return {
        'modDisplayName': t('syncModDisplayName'),
        'enabled': True,
        'column1': [
            templates.createCheckbox(t('syncCheckboxLabel'), _VAR_SYNC_ACTIVE, config.is_sync_enabled(),
                                     tooltip=t('syncCheckboxTooltip')),
        ],
    }


def onModSettingsChanged(linkage, newSettings):
    if linkage != _MOD_LINKAGE or _account_id is None:
        return
    wants_sync = bool(newSettings.get(_VAR_SYNC_ACTIVE))
    config.set_sync_enabled(wants_sync)
    if wants_sync:
        full_reconcile(_account_id)


def register(account_id):
    """Adds the ModsSettingsAPI panel. No-op when the API isn't installed -
    same degrade-quietly convention as importer.register()."""
    global _account_id
    _account_id = account_id
    try:
        from gui.modsSettingsApi import g_modsSettingsApi, templates
    except Exception:
        LOG.info('sync: ModsSettingsAPI not installed - cloud-sync panel disabled')
        return
    template = _build_template(account_id, templates)
    if g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template):
        g_modsSettingsApi.registerCallback(_MOD_LINKAGE, onModSettingsChanged, None)
    else:
        g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template, onModSettingsChanged, None)
    LOG.info('sync: registered cloud-sync panel (enabled=%s)' % config.is_sync_enabled())
