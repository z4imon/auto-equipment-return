# -*- coding: utf-8 -*-
"""Streamer catalog, per-vehicle-type set lookup, and icon caching for the
streamer-recommendation picker on the popover's star button. Mirrors sync.py's
transport style (own urllib2 + threading + BigWorld.callback marshalling), but
every endpoint here is unauthenticated and public - nothing in this module
ever sends or needs a bearer token.

Icon caching lives under config.account_files_dir()/streamer_icons/, keyed by
the streamer's display NAME (sanitized for filesystem safety), not
account_id - account_id is only used to build the download URL itself. Two
files per cached icon: "<name>.bin" (raw bytes) and "<name>.json" (the
remembered Content-Type, since a fixed extension can't be assumed - see
icon_file in the server's streamers.csv, which can be png/jpg/webp/etc.).

Known, accepted limitation: if the server-side icon file is swapped under the
same account_id later, the local cache goes stale until manually cleared -
there is no invalidation mechanism in this iteration."""

import base64
import json
import os
import re
import threading

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
# Icon cache paths
# ---------------------------------------------------------------------------

def _icon_cache_dir():
    return os.path.join(config.account_files_dir(), 'streamer_icons')


def _sanitize_filename(name):
    """A streamer's display name -> a safe filesystem component: keeps
    alphanumerics, replaces everything else (spaces, parens, unicode
    punctuation) with underscores, collapses repeats, and falls back to
    "streamer" if nothing usable survives (empty/None/all-symbol name)."""
    if not name:
        return 'streamer'
    safe = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')
    return safe or 'streamer'


def _cached_icon_bin_path(name):
    return os.path.join(_icon_cache_dir(), '%s.bin' % _sanitize_filename(name))


def _cached_icon_meta_path(name):
    return os.path.join(_icon_cache_dir(), '%s.json' % _sanitize_filename(name))


def _data_uri(bin_path, meta_path):
    with open(bin_path, 'rb') as handle:
        raw = handle.read()
    mime_type = 'application/octet-stream'
    try:
        with open(meta_path, 'r') as handle:
            meta = json.load(handle)
        mime_type = meta.get('contentType') or mime_type
    except Exception:
        pass
    return 'data:%s;base64,%s' % (mime_type, base64.b64encode(raw))


# ---------------------------------------------------------------------------
# Low-level async transport
# ---------------------------------------------------------------------------

def _get_json(path, callback):
    def worker():
        try:
            response = urllib2.urlopen(SERVER_BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS)
            data = json.loads(response.read())
        except Exception:
            LOG.exc('streamers: GET %s failed' % path)
            data = None
        if callback is not None:
            BigWorld.callback(0, lambda: callback(data))

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()


def _download_icon(account_id, name, callback):
    def worker():
        ok = False
        try:
            response = urllib2.urlopen(SERVER_BASE_URL + '/streamers/%s/icon' % account_id,
                                       timeout=REQUEST_TIMEOUT_SECONDS)
            raw = response.read()
            content_type = response.info().gettype()
            directory = _icon_cache_dir()
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(_cached_icon_bin_path(name), 'wb') as handle:
                handle.write(raw)
            with open(_cached_icon_meta_path(name), 'w') as handle:
                json.dump({'contentType': content_type}, handle)
            ok = True
            LOG.info('streamers: icon downloaded for account %s as "%s" (%d bytes, %s)'
                     % (account_id, name, len(raw), content_type))
        except Exception:
            LOG.exc('streamers: icon download failed for account %s (%s)' % (account_id, name))
        if callback is not None:
            BigWorld.callback(0, lambda: callback(ok))

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_streamers(callback):
    """callback([{'accountId': int, 'streamerName': unicode}, ...] or None on
    failure). Always fetched fresh - no session cache, matches the
    hot-loadable CSV this reads from server-side."""
    _get_json('/streamers', callback)


def fetch_vehicle_set(account_id, vehicle_cd, callback):
    """callback(set1, set2) - both None if the streamer has nothing for this
    vehicle type, isn't activated for sharing, or the request failed.
    On-demand only (hover-preview and click-to-apply); never cached."""
    def handle_response(data):
        if not data:
            callback(None, None)
            return
        callback(data.get('set1'), data.get('set2'))
    _get_json('/streamers/%s/vehicle-set/%s' % (account_id, int(vehicle_cd)), handle_response)


def forget_streamer(name):
    """Deletes this streamer's cached icon files, if any - used when a
    previously-selected streamer disappears from the catalog (consent
    revoked, or removed from streamers.csv), so their likeness doesn't
    linger locally forever after that."""
    for path in (_cached_icon_bin_path(name), _cached_icon_meta_path(name)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            LOG.exc('streamers: failed to remove cached icon for "%s"' % name)


def ensure_icon_cached(account_id, name, callback):
    """callback(data_uri_or_None). Reads the local cache if present; downloads
    GET /streamers/{account_id}/icon otherwise. Cached under `name` (the
    streamer's display name, sanitized), not account_id - account_id is only
    used for the download URL itself."""
    bin_path = _cached_icon_bin_path(name)
    meta_path = _cached_icon_meta_path(name)
    if os.path.exists(bin_path) and os.path.exists(meta_path):
        try:
            data_uri = _data_uri(bin_path, meta_path)
            LOG.info('streamers: icon cache hit for "%s" (%d chars)' % (name, len(data_uri)))
            callback(data_uri)
        except Exception:
            LOG.exc('streamers: reading cached icon failed for "%s"' % name)
            callback(None)
        return

    def on_downloaded(ok):
        if not ok:
            callback(None)
            return
        try:
            data_uri = _data_uri(bin_path, meta_path)
            LOG.info('streamers: icon ready for "%s" (%d chars)' % (name, len(data_uri)))
            callback(data_uri)
        except Exception:
            LOG.exc('streamers: encoding downloaded icon failed for "%s"' % name)
            callback(None)

    _download_icon(account_id, name, on_downloaded)
