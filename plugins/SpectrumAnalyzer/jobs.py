# SpectrumAnalyzer - an AudioMuse-AI plugin
# https://github.com/nguyentn1197/audiomuse-ai-spectrum-analyzer-plugin
# Copyright (C) 2026 Nguyen
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root.

"""Worker-side jobs for the SpectrumAnalyzer plugin.

Scan architecture (mirrors the core's run_analysis_task / analyze_album_task)
-----------------------------------------------------------------------------
scan_library_job  parent orchestrator, runs on the *high* queue: plans the
                  scan, enqueues one scan_album_job per album on the *default*
                  queue (so multiple workers process albums in parallel),
                  throttles in-flight children, and tracks done/remaining by
                  reading the children's task_status rows.
scan_album_job    child task: analyzes one album's tracks; also used
                  standalone by the "Re-analyze album" button (mode=force).

Scan modes
----------
changed : skip tracks whose metadata fingerprint is unchanged (default; no
          download for unchanged tracks).
verify  : download everything, but re-analyze only when the audio MD5 changed
          (catches in-place file edits invisible to metadata).
force   : re-download and re-analyze everything.
"""

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid

from plugin.api import (
    config, enqueue, get_db, get_setting, table, logger, save_task_status,
    TASK_STATUS_PENDING, TASK_STATUS_STARTED, TASK_STATUS_PROGRESS,
    TASK_STATUS_SUCCESS, TASK_STATUS_FAILURE, TASK_STATUS_REVOKED,
)
from tasks.mediaserver import get_recent_albums, get_tracks_from_album, download_track

from . import dsp

TASK_TYPE = 'plugin.spectrum_analyzer.scan'
ALBUM_TASK_TYPE = 'plugin.spectrum_analyzer.scan_album'

COUNTER_KEYS = ('analyzed', 'skipped', 'unchanged', 'not_in_score', 'error')
TERMINAL_STATES = (TASK_STATUS_SUCCESS, TASK_STATUS_FAILURE, TASK_STATUS_REVOKED)
POLL_SECONDS = 5

# Raw media-server fields that identify the underlying file cheaply.
# Navidrome's getAlbum tracks keep the raw Subsonic fields (size, suffix,
# bitRate, created, ...); other providers contribute what they have.
_FP_KEYS = ('path', 'Path', 'size', 'Size', 'suffix', 'Container',
            'bitRate', 'bitrate', 'created', 'changed', 'duration')


# ------------------------------------------------- canonical-id translation --
# AudioMuse-AI 3.0 keys `score` by canonical fingerprint ids (fp_...) and maps
# them to native media-server ids in `track_server_map`. Media-server track
# dicts still carry native ids in 'Id'/'id', so everything we store is keyed by
# the canonical id and translated at the edges. On a pre-3.0 core the registry
# module does not exist and both directions collapse to identity/no-op.

def _active_server_id():
    try:
        from plugin.api import active_server_id
    except ImportError:
        return None  # pre-3.0 core: single server
    try:
        return active_server_id()
    except Exception:
        return None


def _list_server_ids():
    """Every configured server's id, default first; [None] on 2.x or failure
    (None = the lone/default server everywhere in this module)."""
    try:
        from plugin.api import list_servers
    except ImportError:
        return [None]
    try:
        ids = [s.get('server_id') for s in list_servers()]
        return [i for i in ids if i] or [None]
    except Exception:
        logger.exception('spectrum_analyzer: listing servers failed')
        return [None]


def _rq_task_id():
    """The running RQ job's id, to key our task_status row by.

    The core janitor fails any top-level non-terminal task row older than a
    grace period whose task_id has no RQ job behind it (Job.fetch by that id).
    A row keyed by a self-invented uuid therefore gets reaped mid-scan; rows
    MUST be keyed by the actual job id. Corollary: routes must not pre-create
    the row either — run_plugin_task fetches the row keyed by the job id
    before the task runs and, when it exists, overwrites it with a bare
    SUCCESS (details wiped) afterwards. The job creates its own row instead.
    """
    try:
        from rq import get_current_job
        job = get_current_job()
        return job.id if job is not None else None
    except Exception:
        return None


def _bind_server(server_id):
    """Context manager binding every media-server call in scope to server_id.

    The core's binding is a contextvar, which does NOT cross RQ job boundaries:
    a child job never inherits the parent's binding, so every job that lists,
    matches or downloads tracks must bind the server it was planned against
    itself. No-op (default server) when server_id is None or on a 2.x core.
    """
    if not server_id:
        return contextlib.nullcontext()
    try:
        from plugin.api import use_server
    except ImportError:
        return contextlib.nullcontext()
    return use_server(server_id)


def _native_to_fp(native_ids, server_id=None):
    """{native_id: canonical id}. Identity mapping on a pre-3.0 core."""
    ids = [str(i) for i in native_ids if i]
    if not ids:
        return {}
    try:
        from tasks.mediaserver.registry import reverse_translate_ids
    except ImportError:
        return {i: i for i in ids}
    try:
        mapped = reverse_translate_ids(ids, server_id=server_id)
    except Exception:
        logger.exception('spectrum_analyzer: native->canonical translation failed')
        return {i: i for i in ids}
    return {i: str(mapped.get(i, i)) for i in ids}


def _fp_to_native(fp_ids, server_id=None):
    """{canonical id: native provider id} for this server; {} on a pre-3.0 core."""
    ids = [str(i) for i in fp_ids if i]
    if not ids:
        return {}
    try:
        from tasks.mediaserver.registry import translate_ids
    except ImportError:
        return {}
    try:
        return {str(k): str(v) for k, v in translate_ids(ids, server_id=server_id).items()}
    except Exception:
        logger.exception('spectrum_analyzer: canonical->native translation failed')
        return {}


def meta_fingerprint(track):
    parts = [f'{k}={track.get(k)}' for k in _FP_KEYS if track.get(k) is not None]
    if not parts:
        return None
    return hashlib.md5('|'.join(parts).encode('utf-8', 'replace')).hexdigest()


def file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _settings():
    return {
        'segment_seconds': int(get_setting('segment_seconds', 90)),
        'drop_db': int(get_setting('drop_db', 40)),
        'img_w': int(get_setting('img_w', 800)),
        'img_h': int(get_setting('img_h', 280)),
    }


def _track_suffix(track):
    s = track.get('suffix') or track.get('Container')
    if s:
        return str(s).lower().lstrip('.')
    p = track.get('Path') or track.get('path') or ''
    return os.path.splitext(p)[1].lstrip('.').lower()


def _track_bitrate(track):
    for k in ('bitRate', 'bitrate', 'Bitrate'):
        v = track.get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _upsert(item_id, info, result, meta_fp, audio_md5):
    tbl = table('results')
    db = get_db()
    cur = db.cursor()
    cur.execute(
        'INSERT INTO ' + tbl + ' (item_id, provider_track_id, server_id, title, '
        'artist, album, album_id, file_path, '
        'suffix, bitrate, meta_fp, audio_md5, sample_rate, seg_offset, seg_seconds, '
        'cutoff_hz, edge_db_khz, shelf_db, verdict, est_source, confidence, details, '
        'container_bits, effective_bits, spectrogram_b64, analysis_rev, deep_eligible, '
        'analyzed_at) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) '
        'ON CONFLICT (item_id) DO UPDATE SET '
        'provider_track_id=EXCLUDED.provider_track_id, server_id=EXCLUDED.server_id, '
        'title=EXCLUDED.title, artist=EXCLUDED.artist, album=EXCLUDED.album, '
        'album_id=EXCLUDED.album_id, file_path=EXCLUDED.file_path, suffix=EXCLUDED.suffix, '
        'bitrate=EXCLUDED.bitrate, meta_fp=EXCLUDED.meta_fp, audio_md5=EXCLUDED.audio_md5, '
        'sample_rate=EXCLUDED.sample_rate, seg_offset=EXCLUDED.seg_offset, '
        'seg_seconds=EXCLUDED.seg_seconds, cutoff_hz=EXCLUDED.cutoff_hz, '
        'edge_db_khz=EXCLUDED.edge_db_khz, shelf_db=EXCLUDED.shelf_db, '
        'verdict=EXCLUDED.verdict, est_source=EXCLUDED.est_source, '
        'confidence=EXCLUDED.confidence, details=EXCLUDED.details, '
        'container_bits=EXCLUDED.container_bits, effective_bits=EXCLUDED.effective_bits, '
        'spectrogram_b64=EXCLUDED.spectrogram_b64, '
        'analysis_rev=EXCLUDED.analysis_rev, deep_eligible=EXCLUDED.deep_eligible, '
        'analyzed_at=now()',
        (
            item_id, info.get('provider_track_id'), info.get('server_id'),
            info.get('title'), info.get('artist'), info.get('album'),
            info.get('album_id'), info.get('file_path'), info.get('suffix'),
            info.get('bitrate'), meta_fp, audio_md5,
            result['sample_rate'], result['seg_offset'], result['seg_seconds'],
            result['cutoff_hz'], result['edge_db_khz'], result['shelf_db'],
            result['verdict'], result['est_source'], result['confidence'],
            result['details'], result.get('container_bits'),
            result.get('effective_bits'), result['spectrogram_b64'],
            result['analysis_rev'], result.get('deep_eligible', True),
        ),
    )
    db.commit()
    cur.close()


def _touch_fingerprint(item_id, meta_fp):
    db = get_db()
    cur = db.cursor()
    cur.execute('UPDATE ' + table('results') + ' SET meta_fp=%s WHERE item_id=%s',
                (meta_fp, item_id))
    db.commit()
    cur.close()


def _existing_rows(item_ids=None):
    """item_id -> (meta_fp, audio_md5, analysis_rev), optionally restricted
    to item_ids."""
    db = get_db()
    cur = db.cursor()
    if item_ids is None:
        cur.execute('SELECT item_id, meta_fp, audio_md5, analysis_rev FROM '
                    + table('results'))
    else:
        cur.execute('SELECT item_id, meta_fp, audio_md5, analysis_rev FROM '
                    + table('results') + ' WHERE item_id = ANY(%s)', (list(item_ids),))
    rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    cur.close()
    return rows


def _backfill_provider_ids():
    """Re-run migrate()'s provider_track_id/server_id backfill from
    track_server_map.

    migrate() is a one-shot that can race the core's v3 migration: on a stuck
    3.0 boot the map table exists but is still empty when the plugin updates
    (the canonicalization commits relabel + map in one transaction, on the
    NEXT boot), so the install-time backfill finds nothing. Scans re-apply it
    — one cheap idempotent UPDATE — healing rows whenever the map has
    entries, including ones added later by a server alignment sweep."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT to_regclass('track_server_map')")
        if cur.fetchone()[0] is None:
            return
        # also repairs rows pointing at a server that no longer exists (the
        # core can re-key a server entry; its map is rebuilt by an alignment
        # sweep, ours follows here)
        cur.execute(
            'UPDATE ' + table('results') + ' r'
            ' SET provider_track_id = m.provider_track_id, server_id = m.server_id'
            ' FROM track_server_map m'
            ' WHERE m.item_id = r.item_id'
            '   AND (r.provider_track_id IS NULL OR r.server_id IS NULL'
            '        OR NOT EXISTS (SELECT 1 FROM music_servers s'
            '                       WHERE s.server_id = r.server_id))')
        healed = cur.rowcount
        db.commit()
        if healed:
            logger.info('spectrum_analyzer: backfilled provider ids for %d rows',
                        healed)
    except Exception:
        db.rollback()
        logger.exception('spectrum_analyzer: provider-id backfill failed')
    finally:
        cur.close()


def _score_ids(item_ids=None):
    db = get_db()
    cur = db.cursor()
    if item_ids is None:
        cur.execute('SELECT item_id FROM score')
    else:
        cur.execute('SELECT item_id FROM score WHERE item_id = ANY(%s)',
                    (list(item_ids),))
    ids = {r[0] for r in cur.fetchall()}
    cur.close()
    return ids


# --------------------------------------------------- task-status plumbing --

def _task_state(task_id):
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT status FROM task_status WHERE task_id = %s', (task_id,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _child_rows(parent_task_id):
    """[(task_id, status, details_dict)] for every child of the parent scan."""
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT task_id, status, details FROM task_status'
                ' WHERE parent_task_id = %s', (parent_task_id,))
    rows = cur.fetchall()
    cur.close()
    out = []
    for child_id, status, details in rows:
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                details = {}
        out.append((child_id, status, details if isinstance(details, dict) else {}))
    return out


def _aggregate(child_rows, base=None):
    total = dict(base) if base else {k: 0 for k in COUNTER_KEYS}
    for _child_id, _status, details in child_rows:
        for k in COUNTER_KEYS:
            v = details.get(k)
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + int(v)
    return total


def _rq_job_lost(job):
    """True when the RQ job backing a child died without writing a terminal
    task_status row (worker crash, job dropped from Redis)."""
    if job is None:
        return False
    try:
        from rq.exceptions import NoSuchJobError
    except ImportError:
        NoSuchJobError = ()
    try:
        state = str(job.get_status(refresh=True) or '')
    except NoSuchJobError:
        # a finished child writes its terminal row before the job expires,
        # so a vanished job behind a non-terminal row means it was lost
        return True
    except Exception:
        return False  # transient Redis error: decide on a later poll
    return state in ('failed', 'stopped', 'canceled')


# ------------------------------------------------------------- album task --

def _analyze_download(track, info, meta_fp, settings, existing, mode, deep=False):
    """Download one track, decide skip/recompute, store. Returns a status tag."""
    item_id = info['item_id']
    tmp = tempfile.mkdtemp(prefix='spectrum_')
    try:
        path = download_track(tmp, track)
        if not path or not os.path.exists(path):
            return 'error'
        md5 = file_md5(path)
        prev = existing.get(item_id)
        rev = dsp.analysis_rev(settings['drop_db'], settings['segment_seconds'])
        if mode != 'force' and prev and prev[1] == md5 and prev[2] == rev:
            # bytes unchanged and analyzed by the current analyzer;
            # just refresh the cheap fingerprint
            _touch_fingerprint(item_id, meta_fp)
            return 'unchanged'
        result = dsp.analyze_file(
            path, suffix=info.get('suffix'), bitrate_kbps=info.get('bitrate'),
            segment_seconds=settings['segment_seconds'], drop_db=settings['drop_db'],
            img_w=settings['img_w'], img_h=settings['img_h'], deep=deep,
        )
        _upsert(item_id, info, result, meta_fp, md5)
        return 'analyzed'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _native_id(track):
    v = track.get('Id') or track.get('id')
    return str(v) if v else None


def _info_from_track(track, album, fp_map=None, server_id=None):
    """fp_map: {native_id: canonical_id} from a per-album _native_to_fp batch;
    item_id is the canonical id (identical to the native id on a v2 core)."""
    native = _native_id(track)
    canonical = (fp_map or {}).get(native, native)
    return {
        'item_id': canonical,
        'provider_track_id': native,
        'server_id': server_id,
        'title': track.get('Name') or track.get('title'),
        'artist': track.get('AlbumArtist') or track.get('artist'),
        'album': track.get('Album') or (album or {}).get('Name'),
        'album_id': (album or {}).get('Id') or track.get('albumId'),
        'file_path': track.get('FilePath') or track.get('Path') or track.get('path'),
        'suffix': _track_suffix(track),
        'bitrate': _track_bitrate(track),
    }


def _album_server(album_id):
    """Server that previously supplied this album's rows (None if unknown)."""
    if not album_id:
        return None
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT server_id FROM ' + table('results')
                    + ' WHERE album_id=%s AND server_id IS NOT NULL LIMIT 1',
                    (album_id,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception:
        return None


def scan_album_job(album_id, album_name, mode='changed', task_id=None,
                   parent_task_id=None, server_id=None):
    """Analyze one album. Child of scan_library_job (which passes the server it
    planned the album against), or standalone for the per-album Re-analyze
    button (mode=force, no parent; server recovered from the stored rows)."""
    task_id = task_id or _rq_task_id() or str(uuid.uuid4())
    settings = _settings()
    rev = dsp.analysis_rev(settings['drop_db'], settings['segment_seconds'])
    counters = {k: 0 for k in COUNTER_KEYS}

    def status(state, progress, detail):
        try:
            save_task_status(task_id, ALBUM_TASK_TYPE, state,
                             parent_task_id=parent_task_id,
                             sub_type_identifier=album_name,
                             progress=progress,
                             details={'mode': mode, 'album': album_name,
                                      'info': detail, **counters})
        except Exception:
            logger.exception('spectrum_analyzer: save_task_status failed')

    if parent_task_id and _task_state(parent_task_id) == TASK_STATUS_REVOKED:
        status(TASK_STATUS_REVOKED, 100, 'parent scan was cancelled')
        return counters

    status(TASK_STATUS_STARTED, 0, 'listing tracks')
    try:
        if parent_task_id is None:  # standalone: parent already did this
            _backfill_provider_ids()
        server_id = server_id or _active_server_id() or _album_server(album_id)
        with _bind_server(server_id):
            tracks = get_tracks_from_album(album_id)
            # one batched native->canonical translation per album, like the
            # core's attach_catalog_item_ids; score and our rows are keyed by
            # canonical ids
            fp_map = _native_to_fp([_native_id(t) for t in tracks], server_id)
            ids = [i for i in fp_map.values() if i]
            score_ids = _score_ids(ids)
            existing = _existing_rows(ids)
            total = max(1, len(tracks))

            for i, track in enumerate(tracks):
                info = _info_from_track(track,
                                        {'Id': album_id, 'Name': album_name},
                                        fp_map, server_id)
                item_id = info['item_id']
                if not item_id:
                    continue
                if item_id not in score_ids:
                    # not analyzed by the core yet -> no score row to attach to
                    # (the on_song_analyzed hook will pick it up later)
                    counters['not_in_score'] += 1
                else:
                    meta_fp = meta_fingerprint(track)
                    prev = existing.get(item_id)
                    if (mode == 'changed' and prev and meta_fp
                            and prev[0] == meta_fp and prev[2] == rev):
                        counters['skipped'] += 1
                    else:
                        try:
                            tag = _analyze_download(track, info, meta_fp,
                                                    settings, existing, mode)
                            counters[tag] += 1
                        except Exception:
                            logger.exception('spectrum_analyzer: failed on %s',
                                             info.get('title'))
                            counters['error'] += 1
                status(TASK_STATUS_PROGRESS, int((i + 1) * 100 / total),
                       info.get('title') or '?')

        status(TASK_STATUS_SUCCESS, 100, 'done')
        return counters
    except Exception as exc:
        status(TASK_STATUS_FAILURE, 100, str(exc))
        raise


# ------------------------------------------------------ parent orchestrator --

def scan_library_job(mode='changed', task_id=None, all_servers=False):
    """Fan the library scan out into one scan_album_job per album.

    Runs on the high queue so it never competes with its own children for a
    default worker. In 'changed' mode albums whose every track is unchanged
    (or not yet in score) are settled here without launching a child.

    all_servers=True (the UI scan buttons) plans the scan across every
    configured media server, launching each album child with the server it was
    listed on; duplicates across servers collapse onto the same canonical id.
    The cron entry point keeps the default False: a schedule's server scope
    already runs the task once per server, bound by the core.
    """
    task_id = task_id or _rq_task_id() or str(uuid.uuid4())
    max_in_flight = max(1, int(getattr(config, 'MAX_QUEUED_ANALYSIS_JOBS', 25)))

    def status(state, progress, detail, extra=None):
        try:
            save_task_status(task_id, TASK_TYPE, state, progress=progress,
                             details={'mode': mode, 'info': detail, **(extra or {})})
        except Exception:
            logger.exception('spectrum_analyzer: save_task_status failed')

    def revoked():
        return _task_state(task_id) == TASK_STATUS_REVOKED

    status(TASK_STATUS_STARTED, 0, 'listing albums')
    try:
        _backfill_provider_ids()
        bound = _active_server_id()  # set when a cron scope bound this run
        server_ids = [bound] if bound or not all_servers else _list_server_ids()
        plan = []  # (server_id, album) pairs, albums listed per server
        for sid in server_ids:
            with _bind_server(sid):
                for album in get_recent_albums(0):  # 0 = every album
                    plan.append((sid, album))
        albums_total = max(1, len(plan))
        albums_skipped = 0
        launched = 0
        launched_jobs = {}  # child task_id -> rq Job, for lost-job reconciliation
        parent_counters = {k: 0 for k in COUNTER_KEYS}
        score_ids = _score_ids() if mode == 'changed' else None
        existing = _existing_rows() if mode == 'changed' else None
        s = _settings()
        rev = dsp.analysis_rev(s['drop_db'], s['segment_seconds'])

        def snapshot(detail):
            rows = _child_rows(task_id)
            done = 0
            for child_id, state, _details in rows:
                if state in TERMINAL_STATES:
                    done += 1
                elif _rq_job_lost(launched_jobs.get(child_id)):
                    # worker died / job dropped: settle the row so the scan
                    # can finish instead of draining forever
                    logger.warning('spectrum_analyzer: child %s lost, marking FAILURE',
                                   child_id)
                    save_task_status(child_id, ALBUM_TASK_TYPE, TASK_STATUS_FAILURE,
                                     parent_task_id=task_id, progress=100,
                                     details={'mode': mode, 'error': 1,
                                              'info': 'worker job failed or was lost'})
                    launched_jobs.pop(child_id, None)
                    done += 1
            agg = _aggregate(rows, base=parent_counters)
            albums_done = albums_skipped + done
            extra = {
                'albums_total': albums_total, 'albums_launched': launched,
                'albums_no_work': albums_skipped, 'albums_done': albums_done,
                'albums_remaining': albums_total - albums_done,
                'servers': len(server_ids), **agg,
            }
            return done, int(albums_done * 100 / albums_total), extra

        def launch(album, album_name, sid):
            child_id = str(uuid.uuid4())
            save_task_status(child_id, ALBUM_TASK_TYPE, TASK_STATUS_PENDING,
                             parent_task_id=task_id, sub_type_identifier=album_name,
                             details={'mode': mode, 'album': album_name,
                                      'info': 'queued'})
            launched_jobs[child_id] = enqueue(
                scan_album_job, album.get('Id'), album_name, mode=mode,
                task_id=child_id, parent_task_id=task_id, server_id=sid)

        for sid, album in plan:
            if revoked():
                logger.info('spectrum_analyzer scan %s cancelled during launch', task_id)
                return
            album_name = album.get('Name') or '?'

            if mode == 'changed':
                # settle all-unchanged albums here, without a child job
                try:
                    with _bind_server(sid):
                        tracks = get_tracks_from_album(album.get('Id'))
                except Exception:
                    logger.exception('spectrum_analyzer: album listing failed: %s',
                                     album_name)
                    parent_counters['error'] += 1
                    albums_skipped += 1
                    done, progress, extra = snapshot(album_name)
                    status(TASK_STATUS_PROGRESS, progress,
                           f'listing failed: {album_name}', extra)
                    continue
                fp_map = _native_to_fp([_native_id(t) for t in tracks], sid)
                pending = 0
                tallies = {'skipped': 0, 'not_in_score': 0}
                for track in tracks:
                    item_id = fp_map.get(_native_id(track))
                    if not item_id:
                        continue
                    if item_id not in score_ids:
                        tallies['not_in_score'] += 1
                    else:
                        prev = existing.get(item_id)
                        fp = meta_fingerprint(track)
                        if prev and fp and prev[0] == fp and prev[2] == rev:
                            tallies['skipped'] += 1
                        else:
                            pending += 1
                if pending == 0:
                    parent_counters['skipped'] += tallies['skipped']
                    parent_counters['not_in_score'] += tallies['not_in_score']
                    albums_skipped += 1
                    # keep progress moving on the settle path too: a library of
                    # mostly-unchanged albums would otherwise show no update
                    # (and look stalled) for the whole planning phase
                    done, progress, extra = snapshot(album_name)
                    status(TASK_STATUS_PROGRESS, progress,
                           f'unchanged: {album_name}', extra)
                    continue

            # throttle: keep at most max_in_flight children unfinished
            while True:
                done, progress, extra = snapshot(f'scanning: {album_name}')
                if launched - done < max_in_flight:
                    break
                if revoked():
                    logger.info('spectrum_analyzer scan %s cancelled while throttled',
                                task_id)
                    return
                status(TASK_STATUS_PROGRESS, progress,
                       f'waiting for workers ({launched - done} albums in flight)',
                       extra)
                time.sleep(POLL_SECONDS)

            launch(album, album_name, sid)
            launched += 1
            done, progress, extra = snapshot(album_name)
            status(TASK_STATUS_PROGRESS, progress, f'queued: {album_name}', extra)

        # all launched; wait for the children to drain
        while True:
            if revoked():
                logger.info('spectrum_analyzer scan %s cancelled while draining',
                            task_id)
                return
            done, progress, extra = snapshot('waiting for album tasks')
            if done >= launched:
                break
            status(TASK_STATUS_PROGRESS, progress,
                   f'{launched - done} albums remaining', extra)
            time.sleep(POLL_SECONDS)

        done, progress, extra = snapshot('done')
        status(TASK_STATUS_SUCCESS, 100, 'done', extra)
        logger.info('spectrum_analyzer scan finished: %s', extra)
        return extra
    except Exception as exc:
        status(TASK_STATUS_FAILURE, 0, str(exc))
        raise


def _clear_deep_pending(item_id):
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute('UPDATE ' + table('results') + ' SET deep_pending=FALSE'
                    ' WHERE item_id=%s', (item_id,))
        db.commit()
        cur.close()
    except Exception:
        logger.exception('spectrum_analyzer: could not clear deep_pending for %s',
                         item_id)


def analyze_track_job(item_id, deep=False):
    """Manual re-run of one song. Always recomputes. deep=True analyzes the
    entire file instead of a segment (dark-master check)."""
    # deep_pending is set by the queueing routes; clear it however this job
    # ends so it can never stick. A plain re-analysis clears it too — that is
    # the escape hatch for a tag orphaned by a lost deep-scan job (the queue
    # routes refuse to re-queue while the tag is set).
    try:
        return _analyze_track(item_id, deep=deep)
    finally:
        _clear_deep_pending(item_id)


def _analyze_track(item_id, deep):
    settings = _settings()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT title, artist, album, album_id, file_path, suffix, bitrate,'
        ' provider_track_id, server_id FROM '
        + table('results') + ' WHERE item_id=%s', (item_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        logger.warning('spectrum_analyzer: no stored row for %s', item_id)
        return

    (title, artist, album, album_id, file_path, suffix, bitrate,
     provider_id, row_server) = row
    server_id = row_server or _active_server_id()
    # the media server speaks native ids: stored provider id, else translate
    # the canonical id, else item_id itself (v2 rows, where they are the same)
    native = (provider_id
              or _fp_to_native([item_id], server_id).get(item_id)
              or item_id)

    with _bind_server(server_id):
        # Prefer fresh metadata from the media server (also refreshes fingerprint)
        track = None
        if album_id:
            try:
                for t in get_tracks_from_album(album_id):
                    if _native_id(t) in (native, item_id):
                        track = t
                        break
            except Exception:
                logger.exception('spectrum_analyzer: album lookup failed for rescan')
        if track is None:
            # minimal item accepted by every provider backend
            track = {'id': native, 'Id': native, 'title': title,
                     'suffix': suffix, 'path': file_path, 'Path': file_path}

        info = _info_from_track(track, {'Id': album_id, 'Name': album},
                                {_native_id(track): item_id}, server_id)
        info['title'] = info['title'] or title
        info['artist'] = info['artist'] or artist
        info['album'] = info['album'] or album
        _analyze_download(track, info, meta_fingerprint(track), settings, {},
                          'force', deep=deep)
    logger.info('spectrum_analyzer: re-analyzed %s%s', info.get('title'),
                ' (deep)' if deep else '')


def scan_changed_task():
    """Entry point for Scheduled Tasks (cron/run-now): incremental scan."""
    return scan_library_job(mode='changed')


def on_song_analyzed(song):
    """Piggyback on core analysis: the audio file is already on disk."""
    try:
        if not get_setting('hook_enabled', True):
            return
        native_id = song.get('item_id')  # v3: the NATIVE provider id
        path = song.get('audio_path')
        if not native_id or not path or not os.path.exists(path):
            return
        # v3 attaches the canonical fp id (what score is keyed by) to the raw
        # media item; on v2 it is absent and the native id IS the score key
        media_item = song.get('media_item') or {}
        item_id = str(media_item.get('_catalog_item_id') or native_id)

        # media_item is the raw provider track dict — the same shape the scans
        # fingerprint — so store the fingerprint a scan would compute and the
        # next 'changed' scan skips this track outright, no download needed
        meta_fp = meta_fingerprint(media_item) or None

        md5 = file_md5(path)
        settings = _settings()
        rev = dsp.analysis_rev(settings['drop_db'], settings['segment_seconds'])
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT audio_md5, analysis_rev FROM ' + table('results')
                    + ' WHERE item_id=%s', (item_id,))
        row = cur.fetchone()
        cur.close()
        if row and row[0] == md5 and row[1] == rev:
            if meta_fp:
                _touch_fingerprint(item_id, meta_fp)  # backfill older hook rows
            return  # same bytes, same analyzer, nothing to do

        meta = song.get('metadata') or {}
        info = {
            'item_id': item_id,
            'provider_track_id': str(native_id),
            'server_id': song.get('server_id'),
            'title': meta.get('title'),
            'artist': meta.get('artist') or meta.get('album_artist'),
            'album': meta.get('album') or meta.get('album_name'),
            'album_id': meta.get('album_id'),
            'file_path': meta.get('file_path'),
            'suffix': os.path.splitext(path)[1].lstrip('.').lower(),
            'bitrate': None,
        }
        result = dsp.analyze_file(
            path, suffix=info['suffix'], bitrate_kbps=None,
            segment_seconds=settings['segment_seconds'], drop_db=settings['drop_db'],
            img_w=settings['img_w'], img_h=settings['img_h'],
        )
        # (if the payload had no media_item — e.g. an older core — meta_fp is
        # NULL and the next 'changed' scan downloads once, notices the MD5
        # matches, and just backfills the fingerprint)
        _upsert(item_id, info, result, meta_fp, md5)
    except Exception:
        logger.exception('spectrum_analyzer: on_song_analyzed failed')
