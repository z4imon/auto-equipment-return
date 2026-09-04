# -*- coding: utf-8 -*-
"""Streamer catalog, per-vehicle-type set lookup, and icon caching for the
streamer-recommendation picker on the popover's star button. Mirrors sync.py's
transport style (own urllib2 + threading + BigWorld.callback marshalling), but
every endpoint here is unauthenticated and public - nothing in this module
ever sends or needs a bearer token.

Icon caching lives under config.account_files_dir()/streamer_icons/, keyed by
the streamer's display NAME (sanitized for filesystem safety), not
account_id - account_id is only used to build the download URL itself. One
file per cached icon, named "<name><ext>" where <ext> is derived from the
download's actual Content-Type (.png/.jpg/etc - see icon_file in the
server's streamers.csv, which can be png/jpg/webp/etc.) via the same stdlib
mimetypes module the server itself uses, so the file is self-describing -
no separate metadata sidecar needed.

Known, accepted limitation: if the server-side icon file is swapped under the
same account_id later, the local cache goes stale until manually cleared -
there is no invalidation mechanism in this iteration."""

import base64
import glob
import hashlib
import json
import mimetypes
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
    punctuation) with underscores, collapses repeats. A name/None with
    nothing Latin-alphanumeric in it (a Cyrillic-, CJK-, or Arabic-only
    handle, or literally none) would otherwise all collapse onto the same
    literal "streamer" fallback - two such streamers would then share one
    cache file, showing one's icon under the other's selection. Falls back
    to a short hash of the original name instead, unique per distinct name
    while still filesystem-safe."""
    if not name:
        return 'streamer'
    safe = re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')
    if safe:
        return safe
    try:
        raw = name.encode('utf-8') if isinstance(name, unicode) else str(name)
    except Exception:
        raw = repr(name)
    return 'streamer_' + hashlib.md5(raw).hexdigest()[:8]


def _cached_icon_glob(name):
    return os.path.join(_icon_cache_dir(), '%s.*' % _sanitize_filename(name))


def _find_cached_icon(name):
    """The path of an already-cached icon for this name, or None. A glob
    rather than a fixed extension, since the extension is only known once a
    download's real Content-Type has been seen (see _download_icon)."""
    matches = glob.glob(_cached_icon_glob(name))
    return matches[0] if matches else None


def _data_uri(path):
    with open(path, 'rb') as handle:
        raw = handle.read()
    mime_type, _ = mimetypes.guess_type(path)
    return 'data:%s;base64,%s' % (mime_type or 'application/octet-stream', base64.b64encode(raw))


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
            extension = mimetypes.guess_extension(content_type) or '.png'
            directory = _icon_cache_dir()
            if not os.path.exists(directory):
                os.makedirs(directory)
            # Drop any previously-cached file for this name under a
            # different extension first (the operator swapped png for jpg,
            # say) - otherwise both would sit on disk and the glob lookup
            # could return either one.
            for stale in glob.glob(_cached_icon_glob(name)):
                try:
                    os.remove(stale)
                except Exception:
                    pass
            path = os.path.join(directory, '%s%s' % (_sanitize_filename(name), extension))
            with open(path, 'wb') as handle:
                handle.write(raw)
            ok = True
            LOG.info('streamers: icon downloaded for account %s as "%s" (%d bytes, %s)'
                     % (account_id, path, len(raw), content_type))
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
    """Deletes this streamer's cached icon file, if any - used when a
    previously-selected streamer disappears from the catalog (consent
    revoked, or removed from streamers.csv), so their likeness doesn't
    linger locally forever after that."""
    for path in glob.glob(_cached_icon_glob(name)):
        try:
            os.remove(path)
        except Exception:
            LOG.exc('streamers: failed to remove cached icon for "%s"' % name)


def ensure_icon_cached(account_id, name, callback):
    """callback(data_uri_or_None). Reads the local cache if present; downloads
    GET /streamers/{account_id}/icon otherwise. Cached under `name` (the
    streamer's display name, sanitized), not account_id - account_id is only
    used for the download URL itself."""
    existing = _find_cached_icon(name)
    if existing is not None:
        try:
            data_uri = _data_uri(existing)
            LOG.info('streamers: icon cache hit for "%s" (%d chars)' % (name, len(data_uri)))
            callback(data_uri)
        except Exception:
            LOG.exc('streamers: reading cached icon failed for "%s"' % name)
            callback(None)
        return

    def on_downloaded(ok):
        path = _find_cached_icon(name) if ok else None
        if path is None:
            callback(None)
            return
        try:
            data_uri = _data_uri(path)
            LOG.info('streamers: icon ready for "%s" (%d chars)' % (name, len(data_uri)))
            callback(data_uri)
        except Exception:
            LOG.exc('streamers: encoding downloaded icon failed for "%s"' % name)
            callback(None)

    _download_icon(account_id, name, on_downloaded)
