# -*- coding: utf-8 -*-
"""Measuring how long equipment work actually takes, in two CSV files.

    <prefs>/mods/z4imon/autoequipmentreturn/metrics/runs.csv
    <prefs>/mods/z4imon/autoequipmentreturn/metrics/ops.csv

WHY THIS EXISTS: every claim about the mod being fast or slow was a feeling.
runs.csv gives one row per run - what it did and how long it took - and ops.csv
one row per operation, so the run's time can be split into the four things it is
actually made of:

    server_ms   waiting for the game server to answer an RPC
    backoff_ms  sleeping through a RES_COOLDOWN retry
    pause_ms    the fixed OP_PAUSE that lets the items cache settle
    client_ms   everything else - the scans in inventory.py, mostly

Only that split says which optimisation is worth doing, and the `variant` column
is what makes a later build comparable to this one. Tag it before every measured
change; without it the old and new rows are indistinguishable afterwards.

TIMER: durations use time.clock(), NOT time.time(). On Windows under Python 2.7
time.time() resolves to about 15.6 ms, and a server call takes 30-100 ms - every
measurement would snap to 0 / 15.6 / 31.2 ms and the whole file would be noise.
time.clock() is a QueryPerformanceCounter wall clock there, with microseconds.
time.time() is used for the timestamp column only, where 15 ms does not matter.

WRITING: rows are buffered in memory and written once per run. This runs on the
game thread, so per-operation file I/O would distort exactly the number being
measured.

FAILING: this is an optional system. Every entry point swallows its exceptions -
a broken metrics line must never abort an equipment run.
"""

import os
import sys
import time

from . import config
from . import __version__
from .log import LOG

# Bump when the columns change; a file whose header no longer matches is rotated
# aside rather than continued with mismatched columns.
SCHEMA = 1

# Names the build these rows came from. CHANGE THIS with every optimisation that
# is meant to be measured - it is the only thing separating before from after.
VARIANT = 'optimization1'

_MAX_BUFFERED_ROWS = 5000
_MAX_FILE_BYTES = 10 * 1024 * 1024

RUN_COLUMNS = (
    'schema', 'ts', 'run_id', 'variant', 'mod_version', 'kind', 'trigger',
    'fleet_size', 'auto_enabled', 'downgrade_enabled',
    'vehicles_planned', 'vehicles_touched', 'installed',
    'demounted_depot', 'demounted_slot_only', 'donor_demounts', 'downgrades',
    'skipped', 'errors', 'interrupted',
    'total_ms', 'server_ms', 'backoff_ms', 'pause_ms', 'client_ms',
    'donor_search_ms', 'donor_search_count', 'rpc_count', 'cooldown_hits',
    'rpc_min_ms',
)

OP_COLUMNS = (
    'schema', 'ts', 'run_id', 'seq', 'kind', 'op',
    'veh_inv_id', 'veh_name', 'veh_tier', 'setup_idx', 'slot_idx',
    'device_cd', 'device_name', 'n_slots',
    'duration_ms', 'code', 'retries', 'backoff_ms',
)

# The totals a caller may hand to end_run(). Anything else is ignored rather
# than written into a column that does not exist.
_RUN_TOTALS = (
    'vehicles_planned', 'vehicles_touched', 'installed',
    'donor_demounts', 'downgrades', 'skipped', 'errors', 'interrupted',
)

# Which op name means what kind of removal. Counted here rather than in apply.py
# because rpc.py already has to tell the two apart to make the call at all -
# asking the run to keep a second tally of the same thing would just be a second
# place for it to go wrong.
_DEMOUNT_COUNTERS = {'demount': 'demounted_depot',
                     'unslot': 'demounted_slot_only'}

# time.clock() is the high-resolution wall clock on Windows in Python 2; on
# anything else it measures CPU time, which is the wrong thing, so fall back.
if sys.platform == 'win32':
    now = time.clock
else:
    now = time.time


def elapsed_ms(started):
    """Milliseconds since a `now()` reading."""
    return (now() - started) * 1000.0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class _Run(object):
    """One measured run. Ops report into it through the module-level functions,
    so no signature in apply.py has to grow a metrics parameter - the same shape
    inventory.py's donor counters already use."""

    def __init__(self, run_id, kind, trigger):
        self.run_id = run_id
        self.kind = kind
        self.trigger = trigger
        self.ts = time.time()
        self.started = now()
        self.seq = 0

        # Current vehicle context, inherited by every op until it changes.
        self.veh_inv_id = None
        self.veh_name = None
        self.veh_tier = None

        self.server_ms = 0.0
        self.backoff_ms = 0.0
        self.pause_ms = 0.0
        self.rpc_count = 0
        self.cooldown_hits = 0
        self.rpc_min_ms = None
        self.demounted_depot = 0
        self.demounted_slot_only = 0


_run = None
_op_rows = []
_run_counter = 0

# Distinguishes runs from different sessions: the counter restarts at 0 every
# time the client does.
_session = int(time.time())


def is_enabled():
    try:
        return config.is_metrics_enabled()
    except Exception:
        return False


def is_running():
    return _run is not None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def start_run(kind, trigger=''):
    """Opens a run. Every caller must close it again in a `finally:`."""
    global _run, _run_counter
    try:
        if not is_enabled():
            return
        if _run is not None:
            # A previous run never closed - write what it has rather than
            # silently folding its ops into this one.
            LOG.warning('metrics: run %s was still open, closing it' % _run.run_id)
            end_run()
        _run_counter += 1
        _run = _Run('%d-%d' % (_session, _run_counter), kind, trigger)
    except Exception:
        LOG.exc('metrics.start_run failed')


def set_vehicle(vehicle):
    """Names the vehicle the following ops belong to."""
    try:
        if _run is None:
            return
        if vehicle is None:
            _run.veh_inv_id = None
            _run.veh_name = None
            _run.veh_tier = None
            return
        _run.veh_inv_id = getattr(vehicle, 'invID', None)
        _run.veh_name = getattr(vehicle, 'userName', None)
        _run.veh_tier = getattr(vehicle, 'level', None)
    except Exception:
        LOG.exc('metrics.set_vehicle failed')


def note_op(op, duration_ms, code=None, retries=0, backoff_ms=0.0, **fields):
    """A server call: its round-trip time, the code it answered with, and how
    much of the time went into cooldown retries rather than the server."""
    try:
        if _run is None:
            return
        _run.rpc_count += 1
        _run.server_ms += duration_ms
        _run.backoff_ms += backoff_ms
        if retries:
            _run.cooldown_hits += retries
        # The floor over a run is the closest thing to a latency reading we get,
        # and it is what makes runs from different sessions comparable.
        if _run.rpc_min_ms is None or duration_ms < _run.rpc_min_ms:
            _run.rpc_min_ms = duration_ms
        counter = _DEMOUNT_COUNTERS.get(op)
        if counter is not None and code >= 0:
            setattr(_run, counter, getattr(_run, counter) + 1)
        _append_op(op, duration_ms, code, retries, backoff_ms, fields)
    except Exception:
        LOG.exc('metrics.note_op failed')


def note_client_op(op, duration_ms, **fields):
    """Work done without touching the server - the inventory scans. Not counted
    into server_ms, so it stays inside client_ms where it belongs."""
    try:
        if _run is None:
            return
        _append_op(op, duration_ms, None, 0, 0.0, fields)
    except Exception:
        LOG.exc('metrics.note_client_op failed')


def note_pause(duration_ms):
    """The fixed OP_PAUSE. Booked separately so it never looks like server time."""
    try:
        if _run is None:
            return
        _run.pause_ms += duration_ms
        _append_op('pause', duration_ms, None, 0, 0.0, {})
    except Exception:
        LOG.exc('metrics.note_pause failed')


def _append_op(op, duration_ms, code, retries, backoff_ms, fields):
    if len(_op_rows) >= _MAX_BUFFERED_ROWS:
        return      # a runaway run must not eat memory
    _run.seq += 1
    veh_inv_id, veh_name, veh_tier = _vehicle_of(fields)
    _op_rows.append({
        'schema': SCHEMA,
        'ts': _stamp(time.time()),
        'run_id': _run.run_id,
        'seq': _run.seq,
        'kind': _run.kind,
        'op': op,
        'veh_inv_id': veh_inv_id,
        'veh_name': veh_name,
        'veh_tier': veh_tier,
        'setup_idx': fields.get('setup_idx'),
        'slot_idx': fields.get('slot_idx'),
        'device_cd': fields.get('device_cd'),
        'device_name': fields.get('device_name'),
        'n_slots': fields.get('n_slots'),
        'duration_ms': duration_ms,
        'code': code,
        'retries': retries,
        'backoff_ms': backoff_ms,
    })


def _vehicle_of(fields):
    """Which vehicle an op belongs to. Usually the one set_vehicle() named, but
    a donor demount addresses a DIFFERENT vehicle - rpc.py knows its inventory
    id and nothing else. Inheriting the run's name there would label the row
    with the wrong tank, so the name is left empty instead of guessed."""
    veh_inv_id = fields.get('veh_inv_id')
    if veh_inv_id is None or veh_inv_id == _run.veh_inv_id:
        return _run.veh_inv_id, _run.veh_name, _run.veh_tier
    return veh_inv_id, fields.get('veh_name'), fields.get('veh_tier')


def end_run(**totals):
    """Closes the run and writes both files. Unknown keyword totals are dropped
    rather than invented into a column."""
    global _run
    run = _run
    _run = None
    try:
        if run is None:
            return
        total_ms = elapsed_ms(run.started)
        row = {
            'schema': SCHEMA,
            'ts': _stamp(run.ts),
            'run_id': run.run_id,
            'variant': VARIANT,
            'mod_version': __version__,
            'kind': run.kind,
            'trigger': run.trigger,
            'auto_enabled': _setting(config.is_auto_enabled),
            'downgrade_enabled': _setting(config.is_downgrade_enabled),
            'total_ms': total_ms,
            'server_ms': run.server_ms,
            'backoff_ms': run.backoff_ms,
            'pause_ms': run.pause_ms,
            # What is left once the server, the retries and the pauses are
            # taken out: our own scanning and planning.
            'client_ms': total_ms - run.server_ms - run.backoff_ms - run.pause_ms,
            'rpc_count': run.rpc_count,
            'cooldown_hits': run.cooldown_hits,
            'rpc_min_ms': run.rpc_min_ms,
            'demounted_depot': run.demounted_depot,
            'demounted_slot_only': run.demounted_slot_only,
        }
        for name in _RUN_TOTALS:
            row[name] = totals.get(name)
        row['donor_search_ms'], row['donor_search_count'] = _donor_stats()
        row['fleet_size'] = _fleet_size()
        _write(_path('runs.csv'), RUN_COLUMNS, [row])
        flush()
    except Exception:
        LOG.exc('metrics.end_run failed')


def _setting(reader):
    try:
        return bool(reader())
    except Exception:
        return None


def _fleet_size():
    """How many vehicles the account owns. Read AFTER total_ms is taken, on
    purpose: the donor search is linear in this number, so it belongs in the
    row - but it is itself a cache query and must not be billed to the run it
    describes."""
    try:
        from . import inventory
        return len(inventory.owned_vehicles())
    except Exception:
        return None


def _donor_stats():
    """inventory's own counters, read at the end of the run. Imported inside the
    function because inventory imports this module for its donor timing."""
    try:
        from . import inventory
        return inventory.donor_search_stats()
    except Exception:
        return None, None


def flush():
    """Writes the buffered op rows. Also called from fini() so a run cut short
    by the client closing still leaves its ops behind."""
    global _op_rows
    rows = _op_rows
    _op_rows = []
    try:
        if rows:
            _write(_path('ops.csv'), OP_COLUMNS, rows)
    except Exception:
        LOG.exc('metrics.flush failed')


# ---------------------------------------------------------------------------
# CSV output
#
# Written by hand rather than through the csv module: that one is byte-oriented
# in Python 2 and cannot take the unicode vehicle and device names. Comma
# separated, UTF-8, no BOM - the shape pandas reads without being told anything.
# ---------------------------------------------------------------------------

def metrics_dir():
    """One folder per VARIANT, so a measured change never appends its rows to
    the build before it. Renaming the variant IS the archiving step - there is
    no separate move to forget."""
    return os.path.join(config.account_files_dir(), 'metrics', VARIANT)


def _path(name):
    return os.path.join(metrics_dir(), name)


def _stamp(epoch):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))


def _cell(value):
    if value is None:
        return u''
    if isinstance(value, bool):
        return u'True' if value else u'False'
    if isinstance(value, float):
        return u'%.3f' % value
    if isinstance(value, str):
        return value.decode('utf-8', 'replace')
    if not isinstance(value, unicode):
        return unicode(value)
    return value


def _csv_line(columns, row):
    cells = []
    for name in columns:
        cell = _cell(row.get(name))
        if any(char in cell for char in u',"\r\n'):
            cell = u'"' + cell.replace(u'"', u'""') + u'"'
        cells.append(cell)
    return u','.join(cells) + u'\n'


def _write(path, columns, rows):
    header = _csv_line(columns, dict((name, name) for name in columns))
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    _rotate_if_stale(path, header)
    # The file's own size decides whether the header still has to go in. NOT
    # handle.tell(): in append mode on Windows that reads 0 until the first
    # write, which quietly put a fresh header in front of every run.
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
    # Binary mode on purpose: text mode would turn every \n into \r\n on
    # Windows and a re-read would show a blank line between rows.
    handle = open(path, 'ab')
    try:
        if needs_header:
            handle.write(header.encode('utf-8'))
        for row in rows:
            handle.write(_csv_line(columns, row).encode('utf-8'))
    finally:
        handle.close()


def _rotate_if_stale(path, header):
    """Moves an existing file aside when it is full or when its header no longer
    matches - appending different columns under an old header would quietly
    produce a file nothing can read correctly."""
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) >= _MAX_FILE_BYTES:
            _rotate(path, 'full')
            return
        handle = open(path, 'rb')
        try:
            first = handle.readline().decode('utf-8', 'replace')
        finally:
            handle.close()
        if first != header:
            _rotate(path, 'schema changed')
    except Exception:
        LOG.exc('metrics: could not check %s' % path)


def _rotate(path, reason):
    backup = path[:-len('.csv')] + '.1.csv'
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(path, backup)
        LOG.info('metrics: rotated %s (%s)' % (os.path.basename(path), reason))
    except Exception:
        LOG.exc('metrics: could not rotate %s' % path)
