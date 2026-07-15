"""Worker-side jobs for the SpectrumAnalyzer plugin.

Scan modes
----------
changed : skip tracks whose metadata fingerprint is unchanged (default; no
          download for unchanged tracks).
verify  : download everything, but re-analyze only when the audio MD5 changed
          (catches in-place file edits invisible to metadata).
force   : re-download and re-analyze everything.
"""

import hashlib
import os
import shutil
import tempfile
import uuid

from plugin.api import (
    get_db, get_setting, table, logger, save_task_status,
    TASK_STATUS_STARTED, TASK_STATUS_PROGRESS, TASK_STATUS_SUCCESS, TASK_STATUS_FAILURE,
)
from tasks.mediaserver import get_recent_albums, get_tracks_from_album, download_track

from . import dsp

TASK_TYPE = 'plugin.spectrum_analyzer.scan'

# Raw media-server fields that identify the underlying file cheaply.
# Navidrome's getAlbum tracks keep the raw Subsonic fields (size, suffix,
# bitRate, created, ...); other providers contribute what they have.
_FP_KEYS = ('path', 'Path', 'size', 'Size', 'suffix', 'Container',
            'bitRate', 'bitrate', 'created', 'changed', 'duration')


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
        'INSERT INTO ' + tbl + ' (item_id, title, artist, album, album_id, file_path, '
        'suffix, bitrate, meta_fp, audio_md5, sample_rate, seg_offset, seg_seconds, '
        'cutoff_hz, edge_db_khz, shelf_db, verdict, est_source, confidence, details, '
        'spectrogram_b64, analyzed_at) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) '
        'ON CONFLICT (item_id) DO UPDATE SET '
        'title=EXCLUDED.title, artist=EXCLUDED.artist, album=EXCLUDED.album, '
        'album_id=EXCLUDED.album_id, file_path=EXCLUDED.file_path, suffix=EXCLUDED.suffix, '
        'bitrate=EXCLUDED.bitrate, meta_fp=EXCLUDED.meta_fp, audio_md5=EXCLUDED.audio_md5, '
        'sample_rate=EXCLUDED.sample_rate, seg_offset=EXCLUDED.seg_offset, '
        'seg_seconds=EXCLUDED.seg_seconds, cutoff_hz=EXCLUDED.cutoff_hz, '
        'edge_db_khz=EXCLUDED.edge_db_khz, shelf_db=EXCLUDED.shelf_db, '
        'verdict=EXCLUDED.verdict, est_source=EXCLUDED.est_source, '
        'confidence=EXCLUDED.confidence, details=EXCLUDED.details, '
        'spectrogram_b64=EXCLUDED.spectrogram_b64, analyzed_at=now()',
        (
            item_id, info.get('title'), info.get('artist'), info.get('album'),
            info.get('album_id'), info.get('file_path'), info.get('suffix'),
            info.get('bitrate'), meta_fp, audio_md5,
            result['sample_rate'], result['seg_offset'], result['seg_seconds'],
            result['cutoff_hz'], result['edge_db_khz'], result['shelf_db'],
            result['verdict'], result['est_source'], result['confidence'],
            result['details'], result['spectrogram_b64'],
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


def _existing_rows():
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT item_id, meta_fp, audio_md5 FROM ' + table('results'))
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.close()
    return rows


def _score_ids():
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT item_id FROM score')
    ids = {r[0] for r in cur.fetchall()}
    cur.close()
    return ids


def _analyze_download(track, info, meta_fp, settings, existing, mode):
    """Download one track, decide skip/recompute, store. Returns a status tag."""
    item_id = info['item_id']
    tmp = tempfile.mkdtemp(prefix='spectrum_')
    try:
        path = download_track(tmp, track)
        if not path or not os.path.exists(path):
            return 'error'
        md5 = file_md5(path)
        prev = existing.get(item_id)
        if mode != 'force' and prev and prev[1] == md5:
            # bytes unchanged; just refresh the cheap fingerprint
            _touch_fingerprint(item_id, meta_fp)
            return 'unchanged'
        result = dsp.analyze_file(
            path, suffix=info.get('suffix'), bitrate_kbps=info.get('bitrate'),
            segment_seconds=settings['segment_seconds'], drop_db=settings['drop_db'],
            img_w=settings['img_w'], img_h=settings['img_h'],
        )
        _upsert(item_id, info, result, meta_fp, md5)
        return 'analyzed'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _info_from_track(track, album):
    return {
        'item_id': track.get('Id') or track.get('id'),
        'title': track.get('Name') or track.get('title'),
        'artist': track.get('AlbumArtist') or track.get('artist'),
        'album': track.get('Album') or (album or {}).get('Name'),
        'album_id': (album or {}).get('Id') or track.get('albumId'),
        'file_path': track.get('FilePath') or track.get('Path') or track.get('path'),
        'suffix': _track_suffix(track),
        'bitrate': _track_bitrate(track),
    }


def scan_library_job(mode='changed', task_id=None):
    """Iterate the whole library album by album."""
    task_id = task_id or str(uuid.uuid4())
    settings = _settings()
    counters = {'analyzed': 0, 'skipped': 0, 'unchanged': 0,
                'not_in_score': 0, 'error': 0}

    def status(state, progress, detail):
        try:
            save_task_status(task_id, TASK_TYPE, state, progress=progress,
                             details={'mode': mode, 'info': detail, **counters})
        except Exception:
            logger.exception('spectrum_analyzer: save_task_status failed')

    status(TASK_STATUS_STARTED, 0, 'listing albums')
    try:
        albums = get_recent_albums(0)  # 0 = every album
        total = max(1, len(albums))
        score_ids = _score_ids()
        existing = _existing_rows()

        for i, album in enumerate(albums):
            album_name = album.get('Name') or '?'
            try:
                tracks = get_tracks_from_album(album.get('Id'))
            except Exception:
                logger.exception('spectrum_analyzer: album listing failed: %s', album_name)
                counters['error'] += 1
                continue

            for track in tracks:
                info = _info_from_track(track, album)
                item_id = info['item_id']
                if not item_id:
                    continue
                if item_id not in score_ids:
                    # not analyzed by the core yet -> no score row to attach to
                    # (the on_song_analyzed hook will pick it up later)
                    counters['not_in_score'] += 1
                    continue
                meta_fp = meta_fingerprint(track)
                prev = existing.get(item_id)
                if mode == 'changed' and prev and meta_fp and prev[0] == meta_fp:
                    counters['skipped'] += 1
                    continue
                try:
                    tag = _analyze_download(track, info, meta_fp, settings, existing, mode)
                    counters[tag] += 1
                except Exception:
                    logger.exception('spectrum_analyzer: failed on %s', info.get('title'))
                    counters['error'] += 1

            status(TASK_STATUS_PROGRESS, int((i + 1) * 100 / total), f'album: {album_name}')

        status(TASK_STATUS_SUCCESS, 100, 'done')
        logger.info('spectrum_analyzer scan finished: %s', counters)
        return counters
    except Exception as exc:
        status(TASK_STATUS_FAILURE, 0, str(exc))
        raise


def analyze_track_job(item_id):
    """Manual re-run of one song. Always recomputes."""
    settings = _settings()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT title, artist, album, album_id, file_path, suffix, bitrate FROM '
        + table('results') + ' WHERE item_id=%s', (item_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        logger.warning('spectrum_analyzer: no stored row for %s', item_id)
        return

    title, artist, album, album_id, file_path, suffix, bitrate = row

    # Prefer fresh metadata from the media server (also refreshes fingerprint)
    track = None
    if album_id:
        try:
            for t in get_tracks_from_album(album_id):
                if (t.get('Id') or t.get('id')) == item_id:
                    track = t
                    break
        except Exception:
            logger.exception('spectrum_analyzer: album lookup failed for rescan')
    if track is None:
        # minimal item accepted by every provider backend
        track = {'id': item_id, 'Id': item_id, 'title': title,
                 'suffix': suffix, 'path': file_path, 'Path': file_path}

    info = _info_from_track(track, {'Id': album_id, 'Name': album})
    info['title'] = info['title'] or title
    info['artist'] = info['artist'] or artist
    info['album'] = info['album'] or album
    _analyze_download(track, info, meta_fingerprint(track), settings, {}, 'force')
    logger.info('spectrum_analyzer: re-analyzed %s', info.get('title'))


def scan_changed_task():
    """Entry point for Scheduled Tasks (cron/run-now): incremental scan."""
    return scan_library_job(mode='changed')


def on_song_analyzed(song):
    """Piggyback on core analysis: the audio file is already on disk."""
    try:
        if not get_setting('hook_enabled', True):
            return
        item_id = song.get('item_id')
        path = song.get('audio_path')
        if not item_id or not path or not os.path.exists(path):
            return

        md5 = file_md5(path)
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT audio_md5 FROM ' + table('results') + ' WHERE item_id=%s',
                    (item_id,))
        row = cur.fetchone()
        cur.close()
        if row and row[0] == md5:
            return  # same bytes, nothing to do

        meta = song.get('metadata') or {}
        settings = _settings()
        info = {
            'item_id': item_id,
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
        # meta_fp stays NULL: the next 'changed' scan sees a mismatch, downloads
        # once, notices the MD5 matches, and just backfills the fingerprint.
        _upsert(item_id, info, result, None, md5)
    except Exception:
        logger.exception('spectrum_analyzer: on_song_analyzed failed')
