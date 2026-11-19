# SpectrumAnalyzer - an AudioMuse-AI plugin
# https://github.com/nguyentn1197/audiomuse-ai-spectrum-analyzer-plugin
# Copyright (C) 2026 Nguyen
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root.

"""SpectrumAnalyzer: spectrum / fake-detection analysis for the whole library.

Pages:  /plugins/spectrum_analyzer/            album-grouped overview + scan buttons
        /plugins/spectrum_analyzer/album?...   per-track results with spectrograms
Worker: library scan (album by album), single-track re-run, on_song_analyzed hook.
Cleanup: results table cascades on DELETE FROM score (the core cleanup task).
"""

import html
from urllib.parse import urlencode

from flask import Blueprint, request, redirect, url_for

from plugin.api import (
    get_db, get_setting, set_setting, table, render_page, manage_plugins_url,
    enqueue, logger,
)

from . import jobs

bp = Blueprint('spectrum_analyzer', __name__)

# The core's plain .btn renders white-on-gray and is barely visible; give
# secondary action buttons an explicit amber/brown look instead.
BTN_STYLE = ('background:#fbbf24;color:#78350f;border:1px solid #d97706;'
             'border-radius:4px;padding:.35rem .8rem;font-weight:600;cursor:pointer;')
BTN_DISABLED_STYLE = ('background:#f3f4f6;color:#9ca3af;border:1px solid #e5e7eb;'
                      'border-radius:4px;padding:.35rem .8rem;cursor:not-allowed;')

VERDICT_STYLE = {
    'CLEAN':            ('#16a34a', 'Full bandwidth'),
    'CONSISTENT_LOSSY': ('#2563eb', 'Lossy, consistent with container'),
    'LOWPASSED':        ('#d97706', 'Low-passed (possibly genuine master)'),
    'UPSAMPLED':        ('#7c3aed', 'Fake hi-res: resampled from a lower-rate source'),
    'UPSCALED':         ('#7c3aed', 'Fake 24-bit: zero-padded 16-bit source'),
    'TRANSCODED_LOSSY': ('#dc2626', 'Lossy re-encoded from lower bitrate'),
    'FAKE_SUSPECT':     ('#dc2626', 'Suspect fake lossless (transcode)'),
    'DAMAGED':          ('#1f2937', 'Damaged file: broken or missing frames, truncated, or '
                                    'checksum mismatch — replace the file'),
    'INCONCLUSIVE':     ('#6b7280', 'Could not be analyzed (decode failure or unsupported format)'),
}
# DAMAGED is deliberately not a suspect verdict: it says nothing about
# authenticity, and it is never deep-scan eligible (bulk deep scans skip it)
SUSPECT_VERDICTS = ('FAKE_SUSPECT', 'TRANSCODED_LOSSY', 'UPSAMPLED', 'UPSCALED')


def migrate(db):
    cur = db.cursor()
    tbl = table('results')
    cur.execute(
        'CREATE TABLE IF NOT EXISTS ' + tbl + ' ('
        # ON UPDATE CASCADE is load-bearing: AudioMuse-AI 3.0 relabels
        # score.item_id in place (native ids -> canonical fp_ ids) and only
        # detaches its own FKs around that UPDATE; with plain NO ACTION our
        # rows would block the core migration, with CASCADE they are relabeled
        # for us (same declaration core uses for track_server_map).
        ' item_id TEXT PRIMARY KEY REFERENCES score (item_id)'
        '   ON UPDATE CASCADE ON DELETE CASCADE,'
        ' provider_track_id TEXT, server_id TEXT,'
        ' title TEXT, artist TEXT, album TEXT, album_id TEXT,'
        ' file_path TEXT, suffix TEXT, bitrate INTEGER,'
        ' meta_fp TEXT, audio_md5 TEXT,'
        ' sample_rate INTEGER, seg_offset REAL, seg_seconds REAL,'
        ' cutoff_hz REAL, edge_db_khz REAL, shelf_db REAL,'
        ' verdict TEXT, est_source TEXT, confidence REAL, details TEXT,'
        ' container_bits INTEGER, effective_bits INTEGER,'
        ' spectrogram_b64 TEXT,'
        ' analysis_rev TEXT,'
        ' analyzed_at TIMESTAMP DEFAULT now())'
    )
    # Prerelease - 0.4.0: bit-depth columns for installs upgrading from older schemas
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS container_bits INTEGER')
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS effective_bits INTEGER')
    # Prerelease - 0.5.0: manual verification flag (verified tracks don't count as suspect)
    cur.execute('ALTER TABLE ' + tbl +
                ' ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE')
    # Prerelease - 0.5.1: set while a deep scan is queued/running, cleared when it finishes
    cur.execute('ALTER TABLE ' + tbl +
                ' ADD COLUMN IF NOT EXISTS deep_pending BOOLEAN NOT NULL DEFAULT FALSE')
    # 0.4.0: analysis-revision stamp; NULL on old rows = stale, re-analyzed on
    # the next changed/verify scan (skip paths require a rev match)
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS analysis_rev TEXT')
    # 0.4.0: deep-scan eligibility, independent of the primary verdict (a
    # decode failure sets it False; old rows default True, preserving today's
    # "deep scan any non-CLEAN track" behavior)
    cur.execute('ALTER TABLE ' + tbl +
                ' ADD COLUMN IF NOT EXISTS deep_eligible BOOLEAN NOT NULL DEFAULT TRUE')
    # Prerelease: timestamp when deep_pending was set; the reconciler's
    # warning-grace reference for claims it cannot settle from the queue
    cur.execute('ALTER TABLE ' + tbl +
                ' ADD COLUMN IF NOT EXISTS deep_pending_since TIMESTAMP')
    # 0.4.0: queue-aware deep claim model. Each deep scan owns a claim id;
    # the state tracks claimed -> launched -> running -> (cleared | failed |
    # outcome_unknown), and the backend records which queue the child went
    # into: 'postgres' is read back from the queue, 'rq' (the retired RQ queue)
    # is only ever quarantined by the reconciler.
    # Existing pending rows are NOT backfilled: a deep_pending with a NULL
    # deep_claim_id is legacy-ambiguous and stays manual-retry-only.
    for col in ('deep_claim_id TEXT', 'deep_task_id TEXT',
                'deep_queue_backend TEXT', 'deep_claim_state TEXT',
                'deep_claimed_at TIMESTAMP', 'deep_completed_claim_id TEXT',
                'deep_completed_at TIMESTAMP', 'deep_claim_detail TEXT'):
        cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS ' + col)
    for name, check in (
            (tbl + '_deep_claim_state_chk',
             'deep_claim_state IS NULL OR deep_claim_state IN'
             " ('claimed', 'launched', 'running', 'failed', 'outcome_unknown')"),
            (tbl + '_deep_queue_backend_chk',
             'deep_queue_backend IS NULL OR deep_queue_backend IN'
             " ('postgres', 'rq')")):
        cur.execute('SELECT 1 FROM pg_constraint WHERE conname = %s', (name,))
        if cur.fetchone() is None:
            cur.execute('ALTER TABLE ' + tbl + ' ADD CONSTRAINT ' + name +
                        ' CHECK (' + check + ')')
    _migrate_v3_ids(db, cur, tbl)
    cur.execute('CREATE INDEX IF NOT EXISTS ' + tbl + '_album_idx ON ' + tbl + ' (album)')
    cur.execute('CREATE INDEX IF NOT EXISTS ' + tbl + '_deep_claim_idx ON ' +
                tbl + ' (deep_claim_id) WHERE deep_pending')
    # optional weekly incremental scan, shipped disabled (admin enables it)
    cur.execute(
        'INSERT INTO cron (name, task_type, cron_expr, enabled) VALUES (%s, %s, %s, FALSE) '
        'ON CONFLICT (task_type) DO NOTHING',
        ('plugin.spectrum_analyzer.scan_changed',
         'plugin.spectrum_analyzer.scan_changed', '0 4 * * 0'),
    )
    # optional deep-claim reconciliation, shipped disabled (admin enables it).
    # The task name predates the claim model and is kept so installs that
    # enabled the old orphan-recovery cron keep working: it now reconciles
    # claim state against the queue and never re-queues anything.
    cur.execute(
        'INSERT INTO cron (name, task_type, cron_expr, enabled) VALUES (%s, %s, %s, FALSE) '
        'ON CONFLICT (task_type) DO NOTHING',
        ('plugin.spectrum_analyzer.recover_deep_pending',
         'plugin.spectrum_analyzer.recover_deep_pending', '0 */3 * * *'),
    )
    db.commit()
    cur.close()


def _migrate_v3_ids(db, cur, tbl):
    """AudioMuse-AI 3.0 compatibility (0.3.0). Idempotent.

    3.0 rewrote score.item_id from native media-server ids to canonical fp_
    ids (track_server_map translates). Steps, in a deliberate order:
    1. add provider_track_id/server_id columns (native id kept for
       download/rescan; item_id is the canonical id from now on),
    2. re-key any native-keyed rows left orphaned by an already-completed core
       migration (only possible in exotic states, e.g. a manually dropped FK),
    3. replace the FK with ON UPDATE CASCADE ON DELETE CASCADE so the core's
       in-place relabel cascades into our rows instead of being blocked by
       them (for users stuck on a failing 3.0 boot migration, the next retry
       succeeds and migrates our data in the same transaction),
    4. backfill provider_track_id/server_id from track_server_map.
    """
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS provider_track_id TEXT')
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS server_id TEXT')

    cur.execute("SELECT to_regclass('track_server_map')")
    has_map = cur.fetchone()[0] is not None

    if has_map:
        # 2. defensive re-key: native-keyed rows that no longer match score but
        # translate via the map (skip if the canonical row already exists)
        cur.execute(
            'UPDATE ' + tbl + ' r SET item_id = m.item_id,'
            ' provider_track_id = COALESCE(r.provider_track_id, m.provider_track_id),'
            ' server_id = COALESCE(r.server_id, m.server_id)'
            ' FROM track_server_map m'
            " WHERE m.provider_track_id = r.item_id AND r.item_id NOT LIKE 'fp\\_%'"
            '   AND NOT EXISTS (SELECT 1 FROM score s WHERE s.item_id = r.item_id)'
            '   AND NOT EXISTS (SELECT 1 FROM ' + tbl + ' r2 WHERE r2.item_id = m.item_id)')

    # 3. FK repair: drop any item_id->score FK that is not ON UPDATE CASCADE,
    # then (re)create it with the cascade. NOT VALID + best-effort VALIDATE so
    # a stray unmatchable row can never brick the plugin update; cascades
    # apply to new writes and to the core relabel either way.
    cur.execute(
        'SELECT con.conname, con.confupdtype FROM pg_constraint con'
        " WHERE con.conrelid = %s::regclass AND con.confrelid = 'score'::regclass"
        "   AND con.contype = 'f'", (tbl,))
    fks = cur.fetchall()
    needs_fk = not fks
    for name, upd in fks:
        if upd != 'c':  # 'c' = ON UPDATE CASCADE
            cur.execute('ALTER TABLE ' + tbl + ' DROP CONSTRAINT "' + name + '"')
            needs_fk = True
    if needs_fk:
        con = tbl + '_item_id_fkey'
        cur.execute(
            'ALTER TABLE ' + tbl + ' ADD CONSTRAINT "' + con + '"'
            ' FOREIGN KEY (item_id) REFERENCES score (item_id)'
            ' ON UPDATE CASCADE ON DELETE CASCADE NOT VALID')
        cur.execute('SAVEPOINT spectrum_fk_validate')
        try:
            cur.execute('ALTER TABLE ' + tbl + ' VALIDATE CONSTRAINT "' + con + '"')
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT spectrum_fk_validate')
        cur.execute('RELEASE SAVEPOINT spectrum_fk_validate')

    if has_map:
        # 4. backfill native ids for rows analyzed before this version
        cur.execute(
            'UPDATE ' + tbl + ' r SET provider_track_id = m.provider_track_id,'
            ' server_id = m.server_id'
            ' FROM track_server_map m'
            ' WHERE r.provider_track_id IS NULL AND m.item_id = r.item_id')


def _esc(v):
    return html.escape('' if v is None else str(v))


# ------------------------------------------------------------ queue guard --

class _QueueRefused(Exception):
    """The core would not take our work onto the queue right now."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _enqueue(*args, **kwargs):
    """`enqueue()`, with a refusal turned into something a route can render.

    The core refuses a plugin enqueue issued from the web process while ANY
    queue job is live — a core analysis, a cleaning run, another plugin's task,
    or one of our own scans — and raises. Unhandled that is a 500 on a button
    click, so every route queues through here. A job already running on a
    worker is exempt (it holds the guard), which is why the parent scan can
    still fan its album children out from inside itself.

    Returns the core's enqueue handle (the queue task id) so a route that
    claimed a deep scan can attach it to the claim.
    """
    try:
        return enqueue(*args, **kwargs)
    except Exception as exc:
        logger.warning('spectrum_analyzer: the core refused to queue our task: %s', exc)
        raise _QueueRefused(getattr(exc, 'user_message', None) or str(exc)) from exc


def _enqueue_bulk(item_ids=None, album=None, select=None, deep=False):
    """Hand a bulk track action to one orchestrator task and return.

    A route CANNOT loop over `enqueue()`: the core consults the queue
    guard on every enqueue made from the web process, and the guard counts live
    `plugin.%` roots — so the first track queued blocks the second. The
    orchestrator runs on the high queue and fans the tracks out from inside a
    worker, where the guard does not apply (`jobs.bulk_analyze_job`). It also
    does the claiming: a route cannot know whether the job it queues will ever
    run, and a `deep_pending` tag written for a job that never starts would
    block those tracks until the recovery TTL.
    """
    _enqueue(jobs.bulk_analyze_job, item_ids=item_ids, album=album,
             select=select, deep=deep, queue='high')


def _queue_refused_page(refusal, back_url=None):
    """A 409 page explaining that nothing was queued, instead of a 500."""
    back = back_url or request.referrer or url_for('spectrum_analyzer.home')
    return render_page(
        '<p style="color:#b45309;font-weight:600;">Nothing was queued.</p>'
        f'<p>{_esc(refusal.message)}</p>'
        '<p style="font-size:.9rem;color:#6b7280;">AudioMuse-AI runs one queue '
        'job at a time. Wait for the running task to finish, or cancel it under '
        'Active Tasks, then try again.</p>'
        f'<p><a href="{_esc(back)}">Back</a></p>',
        title='Spectrum Analyzer'), 409


def _badge(verdict, confidence=None):
    color, label = VERDICT_STYLE.get(verdict, ('#6b7280', verdict or '?'))
    conf = f' {int(confidence * 100)}%' if confidence is not None else ''
    return (f'<span title="{_esc(label)}" style="background:{color};color:#fff;'
            f'border-radius:4px;padding:.1rem .45rem;font-size:.8rem;'
            f'white-space:nowrap;">{_esc(verdict)}{conf}</span>')


def _deep_badge(deep_pending, deep_claim_state):
    """The track-card deep-scan badge, state-aware.

    deep_pending stays true for every active, failed, unknown and legacy
    claim, so the badge — and the disabled Deep-analyze button — key on it;
    the claim state decides what the badge says.
    """
    if not deep_pending:
        return ''
    states = {
        None: ('#6b7280', 'deep scan queued (legacy)',
               'A deep scan was queued before the claim model existed and its '
               'outcome is unknown. Use the retry controls on the overview '
               'page to re-queue it.'),
        'claimed': ('#f59e0b', 'deep scan queued',
                    'A deep scan is waiting in the queue for this track'),
        'launched': ('#f59e0b', 'deep scan queued',
                     'A deep scan is waiting in the queue for this track'),
        'running': ('#3b82f6', 'deep scan running',
                    'A deep scan of this track is in progress'),
        'failed': ('#dc2626', 'deep scan failed',
                   'The deep scan failed. Use the retry controls on the '
                   'overview page to re-queue it.'),
        'outcome_unknown': ('#9333ea', 'deep scan outcome unknown',
                            'The deep scan\'s queue outcome could not be '
                            'established. Re-queueing may run the scan twice; '
                            'use the overview page controls.'),
    }
    color, label, tip = states.get(
        deep_claim_state,
        ('#f59e0b', 'deep scan queued',
         'A deep scan is queued or running for this track'))
    return (f'<span style="background:{color};color:#fff;border-radius:4px;'
            f'padding:.1rem .45rem;font-size:.8rem;margin-left:.4rem;'
            f'white-space:nowrap;" title="{_esc(tip)}">{label}</span>')


def _khz(hz):
    return f'{hz / 1000.0:.1f}' if hz is not None else '?'


# ---------------------------------------------------------------- overview --

@bp.route('/')
def home():
    tbl = table('results')
    q = (request.args.get('q') or '').strip()
    page = max(0, int(request.args.get('p', 0) or 0))
    suspects_only = bool(request.args.get('suspects'))  # legacy links
    verified_only = bool(request.args.get('verified'))
    unverified_only = bool(request.args.get('unv'))
    sel_verdicts = [v for v in request.args.getlist('v') if v in VERDICT_STYLE]
    try:
        min_bad = max(0, int(request.args.get('min_bad', 0) or 0))
    except ValueError:
        min_bad = 0
    if suspects_only:
        min_bad = max(1, min_bad)
    try:
        min_conf = max(0, min(100, int(request.args.get('min_conf', 0) or 0)))
    except ValueError:
        min_conf = 0
    # omitted/empty max_conf is NO upper bound (None); an explicit 0 is a real
    # bound (zero-confidence INCONCLUSIVE results are valid filter targets)
    raw_max = request.args.get('max_conf')
    try:
        max_conf = (max(0, min(100, int(raw_max)))
                    if raw_max not in (None, '') else None)
    except ValueError:
        max_conf = None
    per_page = 100

    # manually verified tracks never count as suspect or lowpassed
    bad_expr = 'COUNT(*) FILTER (WHERE verdict IN %s AND NOT verified)'
    low_expr = "COUNT(*) FILTER (WHERE verdict = 'LOWPASSED' AND NOT verified)"
    inconclusive_expr = ("COUNT(*) FILTER (WHERE verdict = 'INCONCLUSIVE'"
                         ' AND NOT verified)')
    damaged_expr = "COUNT(*) FILTER (WHERE verdict = 'DAMAGED' AND NOT verified)"

    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT COUNT(*), ' + bad_expr + ', ' + low_expr + ', ' + damaged_expr + ', '
        + inconclusive_expr + ','
        ' COUNT(*) FILTER (WHERE verified),'
        ' COUNT(DISTINCT album) FROM ' + tbl, (SUSPECT_VERDICTS,))
    (total, suspects, n_lowpassed, n_damaged, n_inconclusive, n_verified,
     n_albums) = cur.fetchone()

    # deep-claim breakdown for the retry controls: legacy (no claim metadata),
    # failed, outcome_unknown, and live (claimed/launched/running)
    cur.execute(
        'SELECT COUNT(*) FILTER (WHERE deep_pending AND deep_claim_id IS NULL),'
        " COUNT(*) FILTER (WHERE deep_pending AND deep_claim_state = 'failed'),"
        " COUNT(*) FILTER (WHERE deep_pending"
        " AND deep_claim_state = 'outcome_unknown'),"
        " COUNT(*) FILTER (WHERE deep_pending AND deep_claim_state IN"
        " ('claimed', 'launched', 'running')) FROM " + tbl)
    deep_legacy, deep_failed, deep_unknown, deep_live = cur.fetchone()

    # per-status counts for the filter UI (respecting search and confidence/unverified filters)
    count_where = []
    count_params = []
    if q:
        count_where.append('(album ILIKE %s OR artist ILIKE %s OR title ILIKE %s)')
        count_params += [f'%{q}%'] * 3
    if unverified_only:
        count_where.append('NOT verified')
    if min_conf:
        count_where.append('confidence >= %s')
        count_params.append(min_conf / 100.0)
    if max_conf is not None:
        count_where.append('confidence <= %s')
        count_params.append(max_conf / 100.0)
    count_sql = (' WHERE ' + ' AND '.join(count_where)) if count_where else ''
    cur.execute('SELECT verdict, COUNT(*) FROM ' + tbl + count_sql + ' GROUP BY verdict',
                tuple(count_params))
    counts = dict(cur.fetchall())

    having = []
    having_params = []
    if q:
        # match albums containing any song whose album, artist or title
        # matches — counts still cover the whole album
        having.append('bool_or(album ILIKE %s OR artist ILIKE %s OR title ILIKE %s)')
        having_params += [f'%{q}%'] * 3
    if sel_verdicts:
        # albums containing at least one song with any selected status
        cond = 'verdict = ANY(%s)' + (' AND NOT verified' if unverified_only else '')
        having.append('COUNT(*) FILTER (WHERE ' + cond + ') > 0')
        having_params.append(sel_verdicts)
    if min_bad:
        having.append(bad_expr + ' >= %s')
        having_params += [SUSPECT_VERDICTS, min_bad]
    # one combined predicate so a single track must satisfy BOTH bounds
    # (separate FILTERs would let one 90% track and one 0% track pass
    # min_conf=80&max_conf=20 although no single track matches both)
    conf_bounds = []
    conf_params = []
    if min_conf:
        conf_bounds.append('confidence >= %s')
        conf_params.append(min_conf / 100.0)
    if max_conf is not None:
        conf_bounds.append('confidence <= %s')
        conf_params.append(max_conf / 100.0)
    if conf_bounds:
        condition = ' AND '.join(conf_bounds)
        if unverified_only:
            condition += ' AND NOT verified'
        having.append('COUNT(*) FILTER (WHERE ' + condition + ') > 0')
        having_params += conf_params
    if verified_only:
        having.append('COUNT(*) FILTER (WHERE verified) > 0')
    having_sql = (' HAVING ' + ' AND '.join(having)) if having else ''

    params = ([SUSPECT_VERDICTS] + having_params
              + [SUSPECT_VERDICTS, per_page + 1, page * per_page])
    cur.execute(
        'SELECT album, COUNT(*), ' + bad_expr + ', ' + low_expr + ', ' + damaged_expr + ','
        ' COUNT(*) FILTER (WHERE verified),'
        ' COUNT(*) FILTER (WHERE deep_pending),'
        " COUNT(*) FILTER (WHERE deep_pending AND deep_claim_state = 'failed'),"
        " COUNT(*) FILTER (WHERE deep_pending"
        " AND deep_claim_state = 'outcome_unknown'),"
        ' MIN(cutoff_hz), to_char(MAX(analyzed_at), \'DD-MM-YYYY HH24:MI\')'
        ' FROM ' + tbl + ' GROUP BY album'
        + having_sql +
        ' ORDER BY ' + bad_expr + ' DESC, ' + damaged_expr + ' DESC, album'
        ' LIMIT %s OFFSET %s',
        tuple(params))
    rows = cur.fetchall()
    cur.close()
    has_next = len(rows) > per_page
    rows = rows[:per_page]

    scan_buttons = ''.join(
        f'<form method="post" action="{url_for("spectrum_analyzer.scan")}" '
        f'style="display:inline;margin-right:.5rem;">'
        f'<input type="hidden" name="mode" value="{mode}">'
        f'<button type="submit" class="btn btn-primary" title="{_esc(tip)}">{label}</button></form>'
        for mode, label, tip in (
            ('changed', 'Scan library (new / changed)',
             'Album-by-album scan; skips tracks whose file metadata fingerprint is unchanged'),
            ('verify', 'Deep verify (re-hash all)',
             'Downloads every track and re-analyzes only when the audio bytes changed'),
            ('force', 'Force re-analyze all',
             'Recomputes everything, including unchanged files'),
        )) + (
        f'<form method="post" action="{url_for("spectrum_analyzer.deep_rescan_all")}" '
        f'style="display:inline;">'
        f'<button type="submit" class="btn" style="{BTN_STYLE}" '
        f'title="Queue a deep (whole-file) scan for '
        f'every track in the library whose verdict is not CLEAN. Verified and '
        f'already-queued tracks are skipped.">Deep scan all non-CLEAN</button></form>')

    # The reconciler cron never re-queues: an uncertain queue outcome is a
    # badge, and retrying is a deliberate user action. Live scans (claimed /
    # launched / running) have no retry control at all.
    requeue_pending_block = (
        '<p style="font-size:.9rem;margin:.2rem 0;">Deep scans: '
        f'<span style="background:#f59e0b;color:#fff;border-radius:4px;'
        f'padding:.05rem .4rem;">queued/running &times;{deep_live}</span> '
        f'<span style="background:#dc2626;color:#fff;border-radius:4px;'
        f'padding:.05rem .4rem;">failed &times;{deep_failed}</span> '
        f'<span style="background:#9333ea;color:#fff;border-radius:4px;'
        f'padding:.05rem .4rem;">outcome unknown &times;{deep_unknown}</span> '
        f'<span style="background:#6b7280;color:#fff;border-radius:4px;'
        f'padding:.05rem .4rem;">legacy &times;{deep_legacy}</span></p>'
        + (
        f'<form method="post" action="{url_for("spectrum_analyzer.requeue_deep_pending_all")}" '
        f'style="display:inline-block;" '
        f'onsubmit="return confirm(\'This re-queues every FAILED deep scan and every '
        f'LEGACY deep scan (queued before the claim model existed). Scans that are '
        f'still queued or running are left alone. Continue?\');">'
        f'<button type="submit" class="btn" style="{BTN_STYLE}" '
        f'title="Re-queue every deep scan whose claim is marked failed, plus every '
        f'legacy deep scan with no claim metadata. Live scans are never touched.">'
        f'Re-queue failed and legacy deep scans</button></form>'
        '<p style="font-size:.8rem;color:#6b7280;margin:.3rem 0 0;">'
        'Failed scans are safe to re-queue: the original attempt already ended. '
        'Legacy scans predate the claim model, so their outcome is unknown &mdash; '
        'if one is still running you will get a duplicate scan.</p>')
        if (deep_failed or deep_legacy) else '')
    if deep_unknown:
        requeue_pending_block += (
            f'<form method="post" action="{url_for("spectrum_analyzer.requeue_deep_pending_unknown")}" '
            f'style="display:inline-block;margin-left:.6rem;" '
            f'onsubmit="return confirm(\'This re-queues every deep scan whose queue '
            f'outcome could not be established. The original scan may still be '
            f'queued or running somewhere, so re-queueing may run it TWICE. '
            f'Continue?\');">'
            f'<input type="hidden" name="confirm" value="1">'
            f'<button type="submit" class="btn" style="{BTN_STYLE}" '
            f'title="Re-queue every deep scan whose queue outcome is unknown. '
            f'The scan may run twice.">Retry despite unknown outcome '
            f'(&times;{deep_unknown})</button></form>')

    # The bulk buttons hand off to an orchestrator task rather than queueing
    # the tracks themselves (see _enqueue_bulk), so the count is not known here
    # any more: it is decided by the claim the job makes when it starts, and
    # reported on the job's own row under Active Tasks.
    started = request.args.get('started')
    started_msg = ''
    if started in ('deep', 'requeue'):
        what = ('Force re-queue of every track marked deep scan queued'
                if started == 'requeue' else 'Deep scan of all non-CLEAN tracks')
        started_msg = (f'<p style="color:#16a34a;font-weight:600;">{what} started '
                       '&mdash; tracks are queued in batches, follow progress under '
                       'Active Tasks.</p>')

    def _album_tags(ver, pending, pending_failed, pending_unknown):
        tags = ''
        if pending:
            extra = ''
            if pending_failed:
                extra += f' ({pending_failed} failed'
            if pending_unknown:
                extra += (f', {pending_unknown} unknown' if pending_failed
                          else f' ({pending_unknown} unknown')
            if extra:
                extra += ')'
            tags += ('<span style="background:#f59e0b;color:#fff;border-radius:4px;'
                     'padding:.05rem .4rem;font-size:.75rem;margin-left:.4rem;'
                     'white-space:nowrap;" title="Tracks with a deep scan queued, '
                     f'running, failed or of unknown outcome">deep scan '
                     f'&times;{pending}{extra}</span>')
        if ver:
            tags += ('<span style="background:#16a34a;color:#fff;border-radius:4px;'
                      'padding:.05rem .4rem;font-size:.75rem;margin-left:.4rem;'
                      'white-space:nowrap;" title="Manually verified tracks">'
                      f'verified &times;{ver}</span>')
        return tags

    album_rows = ''.join(
        '<tr>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;">'
        f'<a href="{_esc(url_for("spectrum_analyzer.album", name=album or ""))}">{_esc(album) or "(no album)"}</a>'
        f'{_album_tags(ver, pending, pending_failed, pending_unknown)}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{count}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:{"#dc2626" if bad else "#16a34a"};font-weight:600;">{bad}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:{"#d97706" if low else "#6b7280"};font-weight:600;">{low or ""}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:#1f2937;font-weight:600;">{damaged or ""}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:#6b7280;">{ver or ""}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{_khz(min_cut)} kHz</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;">{_esc(last)}</td>'
        '</tr>'
        for album, count, bad, low, damaged, ver, pending, pending_failed,
        pending_unknown, min_cut, last in rows)

    def page_url(p):
        params = {'q': q, 'min_bad': min_bad, 'min_conf': min_conf, 'p': p, 'v': sel_verdicts}
        if max_conf is not None:
            params['max_conf'] = max_conf
        if verified_only:
            params['verified'] = 1
        if unverified_only:
            params['unv'] = 1
        return '?' + _esc(urlencode(params, doseq=True))

    nav = ''
    if page > 0:
        nav += f'<a href="{page_url(page - 1)}" style="margin-right:1rem;">&laquo; prev</a>'
    if has_next:
        nav += f'<a href="{page_url(page + 1)}">next &raquo;</a>'

    body = (
        f'<p><strong>{total}</strong> songs analyzed across <strong>{n_albums}</strong> albums; '
        f'<strong style="color:#dc2626;">{suspects}</strong> suspect '
        f'(fake lossless, transcode or fake hi-res), '
        f'<strong style="color:#d97706;">{n_lowpassed}</strong> lowpassed '
        f'(ambiguous: possibly genuine dark masters), '
        f'<strong style="color:#1f2937;">{n_damaged}</strong> damaged '
        f'(broken or truncated files: replace them), '
        f'<strong style="color:#6b7280;">{n_inconclusive}</strong> inconclusive '
        f'(could not be analyzed: decode failure or unsupported format), '
        f'<strong style="color:#6b7280;">{n_verified}</strong> manually verified '
        f'(excluded from suspect and lowpassed counts).</p>'
        f'{started_msg}'
        f'<div style="margin:.8rem 0;">{scan_buttons}</div>'
        '<p style="font-size:.85rem;color:#6b7280;">Scans run on the worker, album by album; '
        'progress appears under Active Tasks. Already-analyzed tracks are skipped unless '
        'their file changed.</p>'
        f'<div style="margin:.8rem 0;">{requeue_pending_block}</div>'
        f'<form method="get" style="margin:.8rem 0;">'
        f'<div style="display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;">'
        f'<input name="q" value="{_esc(q)}" placeholder="album / artist / song..." '
        f'title="Shows albums containing a match on album, artist or song title">'
        f'<label style="white-space:nowrap;">min suspect tracks '
        f'<input type="number" name="min_bad" min="0" value="{min_bad}" '
        f'style="width:4rem;"></label>'
        f'<label style="white-space:nowrap;" title="Show only albums containing at '
        f'least one track whose verdict confidence is at least this percentage">'
        f'min confidence % <input type="number" name="min_conf" min="0" max="100" '
        f'value="{min_conf}" style="width:4rem;"></label>'
        f'<label style="white-space:nowrap;margin-left:.6rem;" title="Show only albums containing at '
        f'least one track whose verdict confidence is at most this percentage">'
        f'max confidence % <input type="number" name="max_conf" min="0" max="100" '
        f'value="{"" if max_conf is None else max_conf}" style="width:4rem;"></label>'
        f'<label style="white-space:nowrap;"><input type="checkbox" name="verified" '
        f'value="1" {"checked" if verified_only else ""}> only albums with verified tracks</label>'
        f'<button type="submit" class="btn" style="{BTN_STYLE}">Filter</button></div>'
        f'<div style="display:flex;gap:.2rem .6rem;align-items:center;flex-wrap:wrap;'
        f'margin-top:.5rem;">'
        f'<span style="font-size:.85rem;color:#6b7280;margin-right:.2rem;" '
        f'title="Show only albums containing at least one song with any of the '
        f'selected statuses">albums containing:</span>'
        + ''.join(
            f'<label style="display:inline-flex;align-items:center;gap:.25rem;'
            f'font-size:.85rem;white-space:nowrap;cursor:pointer;" title="{_esc(vlabel)}">'
            f'<input type="checkbox" name="v" value="{v}" '
            f'{"checked" if v in sel_verdicts else ""}>'
            f'<span style="background:{color};color:#fff;border-radius:4px;'
            f'padding:.05rem .4rem;">{v} ({counts.get(v, 0)})</span></label>'
            for v, (color, vlabel) in VERDICT_STYLE.items())
        + f'<label style="white-space:nowrap;font-size:.85rem;margin-left:.4rem;" '
          f'title="When matching the selected statuses, ignore manually verified tracks">'
          f'<input type="checkbox" name="unv" value="1" '
          f'{"checked" if unverified_only else ""}> unverified tracks only</label>'
        '</div></form>'
        '<table style="border-collapse:collapse;width:100%;font-size:.95rem;">'
        '<tr><th style="text-align:left;padding:.3rem .6rem;">Album</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Tracks</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Suspect</th>'
        '<th style="text-align:right;padding:.3rem .6rem;" '
        'title="LOWPASSED verdicts: limited bandwidth without a clear encoder '
        'signature — possibly genuine dark masters, worth a deep scan">Lowpassed</th>'
        '<th style="text-align:right;padding:.3rem .6rem;" '
        'title="DAMAGED verdicts: broken, missing or truncated frames — the file '
        'needs replacing, a rescan won\'t fix it">Damaged</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Verified</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Lowest cutoff</th>'
        '<th style="text-align:left;padding:.3rem .6rem;">Last analyzed</th></tr>'
        f'{album_rows}</table>'
        f'<div style="margin-top:1rem;">{nav}</div>'
    )
    return render_page(body, title='Spectrum Analyzer')


# ------------------------------------------------------------- album view --

@bp.route('/album')
def album():
    tbl = table('results')
    name = request.args.get('name') or ''
    sel_verdicts = [v for v in request.args.getlist('v') if v in VERDICT_STYLE]
    unverified_only = bool(request.args.get('unv'))
    try:
        min_conf = max(0, min(100, int(request.args.get('min_conf', 0) or 0)))
    except ValueError:
        min_conf = 0
    # omitted/empty max_conf is NO upper bound (None); an explicit 0 is a real
    # bound (zero-confidence INCONCLUSIVE results are valid filter targets)
    raw_max = request.args.get('max_conf')
    try:
        max_conf = (max(0, min(100, int(raw_max)))
                    if raw_max not in (None, '') else None)
    except ValueError:
        max_conf = None
    filtered = bool(sel_verdicts or min_conf or max_conf is not None
                    or unverified_only)

    where = ['album = %s']
    params = [name]
    if sel_verdicts:
        where.append('verdict = ANY(%s)')
        params.append(sel_verdicts)
    if min_conf:
        where.append('confidence >= %s')
        params.append(min_conf / 100.0)
    if max_conf is not None:
        where.append('confidence <= %s')
        params.append(max_conf / 100.0)
    if unverified_only:
        where.append('NOT verified')

    db = get_db()
    cur = db.cursor()
    # album_id is looked up independent of the filter above so "Re-analyze
    # album" still works even when the filter hides every visible track
    cur.execute('SELECT album_id FROM ' + tbl +
                ' WHERE album = %s AND album_id IS NOT NULL LIMIT 1', (name,))
    r = cur.fetchone()
    album_id = r[0] if r else None

    cur.execute('SELECT COUNT(*) FROM ' + tbl + ' WHERE album = %s', (name,))
    total_tracks = cur.fetchone()[0]

    # per-status counts for the album filter (respecting min/max confidence and verif)
    count_where = ['album = %s']
    count_params = [name]
    if unverified_only:
        count_where.append('NOT verified')
    if min_conf:
        count_where.append('confidence >= %s')
        count_params.append(min_conf / 100.0)
    if max_conf is not None:
        count_where.append('confidence <= %s')
        count_params.append(max_conf / 100.0)
    count_sql = ' WHERE ' + ' AND '.join(count_where)
    cur.execute('SELECT verdict, COUNT(*) FROM ' + tbl + count_sql + ' GROUP BY verdict',
                tuple(count_params))
    album_counts = dict(cur.fetchall())

    cur.execute(
        'SELECT item_id, title, artist, suffix, bitrate, sample_rate, cutoff_hz,'
        ' edge_db_khz, verdict, est_source, confidence, details,'
        ' to_char(analyzed_at, \'DD-MM-YYYY HH24:MI\'), spectrogram_b64, album_id,'
        ' container_bits, effective_bits, verified, deep_pending, deep_eligible,'
        ' deep_claim_state'
        ' FROM ' + tbl + ' WHERE ' + ' AND '.join(where) + ' ORDER BY title',
        tuple(params))
    rows = cur.fetchall()
    cur.close()

    cards = []
    for (item_id, title, artist, suffix, bitrate, sr, cutoff, edge, verdict,
         est, conf, details, when, png, _aid, cbits, ebits, verified,
         deep_pending, deep_eligible, deep_claim_state) in rows:
        if cbits and ebits and ebits < cbits:
            bits = (f' / <span style="color:#dc2626;font-weight:600;" '
                    f'title="effective bits / container bits">{ebits}&rarr;{cbits} bit</span>')
        elif cbits:
            bits = f' / {cbits} bit'
        else:
            bits = ''
        fmt = f'{_esc(suffix)}{f" {bitrate}k" if bitrate else ""} / {sr or "?"} Hz{bits}'
        img = (f'<img src="data:image/png;base64,{png}" alt="spectrogram" loading="lazy" '
               f'style="max-width:100%;height:auto;border:1px solid #ddd;border-radius:4px;">'
               ) if png else '<em>no spectrogram stored</em>'
        cards.append(
            '<div style="margin:1rem 0;padding:.8rem;border:1px solid #ddd;border-radius:6px;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;'
            'flex-wrap:wrap;gap:.5rem;">'
            f'<div><input type="checkbox" form="bulk-form" name="item_id" '
            f'value="{_esc(item_id)}" class="bulk-select-cb" style="margin-right:.5rem;">'
            f'<strong>{_esc(title)}</strong> <span style="color:#6b7280;">'
            f'{_esc(artist)} &middot; {fmt}</span></div>'
            f'<div>{_badge(verdict, conf)}{_deep_badge(deep_pending, deep_claim_state)}'
            + f'<form method="post" action="{url_for("spectrum_analyzer.verify", item_id=item_id)}" '
            f'style="display:inline;margin-left:.6rem;">'
            f'<label style="white-space:nowrap;font-size:.85rem;" '
            f'title="Mark as manually checked: verified tracks are excluded from suspect counts">'
            f'<input type="checkbox" name="verified" value="1" '
            f'{"checked" if verified else ""} onchange="this.form.submit()"> '
            'verified</label></form>'
            f'<form method="post" action="{url_for("spectrum_analyzer.rescan", item_id=item_id)}" '
            f'style="display:inline;margin-left:.6rem;">'
            f'<button type="submit" class="btn" style="{BTN_STYLE}" '
            f'title="Force re-download and re-analyze this song">'
            'Re-analyze</button></form>'
            f'<form method="post" action="{url_for("spectrum_analyzer.deep_rescan", item_id=item_id)}" '
            f'style="display:inline;margin-left:.4rem;">'
            f'<button type="submit" class="btn" '
            f'{"disabled" if deep_pending or not deep_eligible else ""} '
            f'style="{BTN_DISABLED_STYLE if deep_pending or not deep_eligible else BTN_STYLE}" '
            f'title="{"A deep scan is already queued for this track" if deep_pending else "This track could not be decoded at all — a deep scan would not add anything" if not deep_eligible else "Analyze the ENTIRE file (not a segment) and track the spectral edge over time: an edge that follows the music means a genuine dark master, a constant wall means a resampler/encoder"}">'
            'Deep analyze</button></form></div></div>'
            f'<p style="margin:.4rem 0;font-size:.9rem;">Cutoff <strong>{_khz(cutoff)} kHz</strong>'
            f' &middot; edge {edge if edge is not None else "?"} dB/kHz'
            f' &middot; {_esc(est)} &middot; analyzed {_esc(when)}</p>'
            f'{img}'
            f'<details style="margin-top:.4rem;font-size:.8rem;color:#6b7280;">'
            f'<summary>raw metrics</summary><pre style="white-space:pre-wrap;">{_esc(details)}</pre>'
            '</details></div>'
        )

    rescan_all = (
        f'<form method="post" action="{url_for("spectrum_analyzer.rescan_album")}" '
        f'style="display:inline;">'
        f'<input type="hidden" name="name" value="{_esc(name)}">'
        f'<input type="hidden" name="album_id" value="{_esc(album_id or "")}">'
        f'<button type="submit" class="btn btn-primary" '
        f'title="Force re-download and re-analyze every track in this album">'
        'Re-analyze album</button></form>'
        f'<form method="post" action="{url_for("spectrum_analyzer.deep_rescan_album")}" '
        f'style="display:inline;margin-left:.4rem;">'
        f'<input type="hidden" name="name" value="{_esc(name)}">'
        f'<button type="submit" class="btn" style="{BTN_STYLE}" '
        f'title="Queue a deep (whole-file) scan for every track whose verdict is not '
        f'CLEAN. Verified and already-queued tracks are skipped.">'
        'Deep scan all non-CLEAN</button></form>') if rows else ''

    filter_form = (
        f'<form method="get" style="margin:.6rem 0;">'
        f'<input type="hidden" name="name" value="{_esc(name)}">'
        f'<div style="display:flex;gap:.2rem .6rem;align-items:center;flex-wrap:wrap;">'
        f'<span style="font-size:.85rem;color:#6b7280;margin-right:.2rem;" '
        f'title="Show only tracks with any of the selected statuses">status:</span>'
        + ''.join(
            f'<label style="display:inline-flex;align-items:center;gap:.25rem;'
            f'font-size:.85rem;white-space:nowrap;cursor:pointer;" title="{_esc(vlabel)}">'
            f'<input type="checkbox" name="v" value="{v}" '
            f'{"checked" if v in sel_verdicts else ""}>'
            f'<span style="background:{color};color:#fff;border-radius:4px;'
            f'padding:.05rem .4rem;">{v} ({album_counts.get(v, 0)})</span></label>'
            for v, (color, vlabel) in VERDICT_STYLE.items())
        + f'<label style="white-space:nowrap;font-size:.85rem;margin-left:.4rem;" '
          f'title="Show only tracks with verdict confidence at least this percentage">'
          f'min confidence % <input type="number" name="min_conf" min="0" max="100" '
          f'value="{min_conf}" style="width:4rem;"></label>'
        + f'<label style="white-space:nowrap;font-size:.85rem;margin-left:.4rem;" '
          f'title="Show only tracks with verdict confidence at most this percentage">'
          f'max confidence % <input type="number" name="max_conf" min="0" max="100" '
          f'value="{"" if max_conf is None else max_conf}" style="width:4rem;"></label>'
        + f'<label style="white-space:nowrap;font-size:.85rem;margin-left:.4rem;" '
          f'title="Hide manually verified tracks">'
          f'<input type="checkbox" name="unv" value="1" '
          f'{"checked" if unverified_only else ""}> unverified tracks only</label>'
        + f'<button type="submit" class="btn" style="{BTN_STYLE}">Filter</button>'
        '</div></form>'
        + (f'<p style="font-size:.85rem;color:#6b7280;">Showing {len(rows)} of '
           f'{total_tracks} tracks matching filter.</p>' if filtered else '')
    )

    bulk_actions = (
        f'<form id="bulk-form" method="post" '
        f'action="{url_for("spectrum_analyzer.rescan_selected")}" style="margin:.6rem 0;">'
        '<label style="font-size:.85rem;margin-right:.8rem;cursor:pointer;">'
        '<input type="checkbox" onclick="var ck=this.checked;'
        'document.querySelectorAll(\'.bulk-select-cb\').forEach('
        'function(c){c.checked=ck;});"> select all shown</label>'
        f'<button type="submit" class="btn" style="{BTN_STYLE}" '
        f'formaction="{url_for("spectrum_analyzer.rescan_selected")}" '
        'onclick="var n=document.querySelectorAll(\'.bulk-select-cb:checked\').length;'
        'if(!n){alert(\'Select at least one track first.\');return false;}'
        'return confirm(\'Re-analyze \'+n+\' selected track(s)?\');" '
        'title="Force re-download and re-analyze the checked tracks">'
        'Re-analyze selected</button> '
        f'<button type="submit" class="btn" style="{BTN_STYLE}" '
        f'formaction="{url_for("spectrum_analyzer.deep_rescan_selected")}" '
        'onclick="var n=document.querySelectorAll(\'.bulk-select-cb:checked\').length;'
        'if(!n){alert(\'Select at least one track first.\');return false;}'
        'return confirm(\'Deep-scan \'+n+\' selected track(s)? Already-queued or '
        'non-eligible tracks are skipped.\');" '
        'title="Queue a deep (whole-file) scan for the checked tracks">'
        'Deep-scan selected</button>'
        '</form>'
    ) if rows else ''

    body = (
        f'<p><a href="{url_for("spectrum_analyzer.home")}">&laquo; all albums</a></p>'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'flex-wrap:wrap;gap:.5rem;">'
        f'<h3 style="margin:.2rem 0;">{_esc(name) or "(no album)"}</h3>{rescan_all}</div>'
        f'{filter_form}{bulk_actions}'
        + (''.join(cards) or ('<p>No tracks match this filter.</p>' if filtered
                              else '<p>No analyzed tracks in this album.</p>'))
    )
    return render_page(body, title='Spectrum Analyzer')


# ---------------------------------------------------------------- actions --

@bp.route('/scan', methods=['POST'])
def scan():
    mode = request.form.get('mode', 'changed')
    if mode not in ('changed', 'verify', 'force'):
        mode = 'changed'
    # the orchestrator lives on the high queue so its per-album children
    # (default queue) can be picked up by every default worker in parallel;
    # all_servers: a manual scan covers every configured media server, not
    # just the default one (no-op on a single-server install).
    # No task row is created here: the job keys its own row by the id the core
    # queued it under (jobs._current_task_id), which is the only id the core's
    # own bookkeeping — its Cancel button, its orphan sweeps — agrees with
    try:
        _enqueue(jobs.scan_library_job, mode=mode, all_servers=True, queue='high')
    except _QueueRefused as exc:
        return _queue_refused_page(exc, url_for('spectrum_analyzer.home'))
    return render_page(
        f'<p>Library scan started (mode: <strong>{_esc(mode)}</strong>), '
        'covering every configured media server. '
        'Albums are dispatched as parallel worker tasks; follow overall progress '
        'and per-album sub-tasks under Active Tasks.</p>'
        f'<p><a href="{url_for("spectrum_analyzer.home")}">Back</a></p>',
        title='Spectrum Analyzer')


@bp.route('/rescan/<item_id>', methods=['POST'])
def rescan(item_id):
    try:
        _enqueue(jobs.analyze_track_job, item_id, queue='high')
    except _QueueRefused as exc:
        return _queue_refused_page(exc)
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/deep_rescan/<item_id>', methods=['POST'])
def deep_rescan(item_id):
    # claim first so the page shows "deep scan queued" immediately; the child
    # completes the claim when the analysis lands. The NOT deep_pending guard
    # inside the claim makes the button idempotent: repeat clicks while a scan
    # is queued enqueue nothing.
    claim_id = jobs.claim_deep(item_id)
    if claim_id:
        try:
            handle = _enqueue(jobs.analyze_track_job, item_id, deep=True,
                              claim_id=claim_id, queue='high')
        except _QueueRefused as exc:
            jobs._clear_deep_claims_many([(item_id, claim_id)])
            return _queue_refused_page(exc)
        jobs._attach_deep_task(item_id, claim_id, handle)
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/album/rescan_selected', methods=['POST'])
def rescan_selected():
    item_ids = request.form.getlist('item_id')
    if not item_ids:
        return redirect(request.referrer or url_for('spectrum_analyzer.home'))
    try:
        _enqueue_bulk(item_ids=item_ids)
    except _QueueRefused as exc:
        return _queue_refused_page(exc)
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/album/deep_rescan_selected', methods=['POST'])
def deep_rescan_selected():
    item_ids = request.form.getlist('item_id')
    if not item_ids:
        return redirect(request.referrer or url_for('spectrum_analyzer.home'))
    # the tag-and-collect (same NOT deep_pending AND deep_eligible guard as the
    # single-track button) happens inside the orchestrator, so a job that never
    # starts cannot leave tags behind
    try:
        _enqueue_bulk(item_ids=item_ids, deep=True)
    except _QueueRefused as exc:
        return _queue_refused_page(exc)
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/deep_pending/requeue_all', methods=['POST'])
def requeue_deep_pending_all():
    # select='requeue_all': re-claims rows with no claim metadata at all
    # (legacy) or whose claim is already marked failed. Live claims match the
    # select, so this can never replace a scan that may still be running. Not
    # automatic, because re-queuing one that is merely slow means analyzing
    # it twice.
    try:
        _enqueue_bulk(select='requeue_all', deep=True)
    except _QueueRefused as exc:
        return _queue_refused_page(exc, url_for('spectrum_analyzer.home'))
    return redirect(url_for('spectrum_analyzer.home', started='requeue'))


@bp.route('/deep_pending/requeue_unknown', methods=['POST'])
def requeue_deep_pending_unknown():
    # select='requeue_unknown': re-claims rows whose claim has no attached
    # queue task, or whose outcome the reconciler could not establish. The
    # child may in fact still be queued or running somewhere, so this route
    # only accepts an explicit duplicate-risk confirmation: the scan may run
    # twice.
    if request.form.get('confirm') != '1':
        return redirect(request.referrer or url_for('spectrum_analyzer.home'))
    try:
        _enqueue_bulk(select='requeue_unknown', deep=True)
    except _QueueRefused as exc:
        return _queue_refused_page(exc, url_for('spectrum_analyzer.home'))
    return redirect(url_for('spectrum_analyzer.home', started='requeue'))


@bp.route('/album/deep_all', methods=['POST'])
def deep_rescan_album():
    name = request.form.get('name') or ''
    try:
        _enqueue_bulk(select='non_clean', album=name, deep=True)
    except _QueueRefused as exc:
        return _queue_refused_page(exc)
    return redirect(request.referrer or url_for('spectrum_analyzer.album', name=name))


@bp.route('/deep_all', methods=['POST'])
def deep_rescan_all():
    try:
        _enqueue_bulk(select='non_clean', deep=True)
    except _QueueRefused as exc:
        return _queue_refused_page(exc, url_for('spectrum_analyzer.home'))
    return redirect(url_for('spectrum_analyzer.home', started='deep'))


@bp.route('/verify/<item_id>', methods=['POST'])
def verify(item_id):
    db = get_db()
    cur = db.cursor()
    cur.execute('UPDATE ' + table('results') + ' SET verified=%s WHERE item_id=%s',
                (bool(request.form.get('verified')), item_id))
    db.commit()
    cur.close()
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/album/rescan', methods=['POST'])
def rescan_album():
    name = request.form.get('name') or ''
    album_id = (request.form.get('album_id') or '').strip()
    if album_id:
        # no pre-created task row: the job keys its own row by the id the core
        # queued it under (jobs._current_task_id)
        try:
            _enqueue(jobs.scan_album_job, album_id, name, mode='force')
        except _QueueRefused as exc:
            return _queue_refused_page(exc)
    else:
        # no album id stored (e.g. hook-inserted rows): re-run track by track
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT item_id FROM ' + table('results') + ' WHERE album = %s',
                    (name,))
        item_ids = [r[0] for r in cur.fetchall()]
        cur.close()
        if item_ids:
            try:
                _enqueue_bulk(item_ids=item_ids)
            except _QueueRefused as exc:
                return _queue_refused_page(exc)
    return redirect(request.referrer
                    or url_for('spectrum_analyzer.album', name=name))


# --------------------------------------------------------------- settings --

@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        for key, lo, hi, dflt in (('segment_seconds', 20, 600, 90),
                                  ('drop_db', 20, 70, 40),
                                  ('img_w', 400, 2000, 800),
                                  ('img_h', 160, 800, 280),
                                  ('deep_pending_warning_hours', 1, 168, 2)):  # 1h-7d, default 2h
            try:
                set_setting(key, max(lo, min(hi, int(request.form.get(key, dflt)))))
            except (TypeError, ValueError):
                set_setting(key, dflt)
        set_setting('hook_enabled', bool(request.form.get('hook_enabled')))
        set_setting('skip_clean_spectrograms', bool(request.form.get('skip_clean_spectrograms')))
        return redirect(manage_plugins_url())

    def num(key, dflt, label, hint):
        return (f'<label style="display:block;margin:.6rem 0;">{label} '
                f'<input type="number" name="{key}" value="{get_setting(key, dflt)}" '
                f'style="width:6rem;"> <span style="color:#6b7280;font-size:.85rem;">'
                f'{hint}</span></label>')

    hook = 'checked' if get_setting('hook_enabled', True) else ''
    skip_clean = 'checked' if get_setting('skip_clean_spectrograms', False) else ''
    body = (
        '<form method="post">'
        + num('segment_seconds', 90, 'Analyzed segment (s)',
              'total budget, split across several windows spread through the track')
        + num('drop_db', 40, 'Cutoff threshold (dB)',
              'level drop below the 1-8 kHz reference that counts as "no content"')
        + num('img_w', 800, 'Spectrogram width (px)', '')
        + num('img_h', 280, 'Spectrogram height (px)',
              'bigger images mean more base64 in the database')
        + num('deep_pending_warning_hours',
              get_setting('deep_pending_warning_hours',
                          get_setting('deep_orphan_ttl_hours', 2)),
              'Deep scan warning threshold (hours)',
              'hours before the reconciler flags a deep scan whose queue outcome it cannot establish; it only warns, it never re-queues')
        + f'<label style="display:block;margin:.6rem 0;">'
          f'<input type="checkbox" name="hook_enabled" {hook}> '
          'Also analyze songs automatically during core analysis (on_song_analyzed hook)</label>'
        + f'<label style="display:block;margin:.6rem 0;">'
          f'<input type="checkbox" name="skip_clean_spectrograms" {skip_clean}> '
          'Skip storing a spectrogram for CLEAN tracks (saves CPU/DB space; audio isn\'t '
          'retained after analysis, so a skipped spectrogram can\'t be rendered later)</label>'
        '<button type="submit" class="btn btn-primary" style="margin-top:1rem;">Save</button>'
        '</form>')
    return render_page(body, title='Spectrum Analyzer Settings')


def register(ctx):
    ctx.on_install(migrate)
    ctx.add_blueprint(bp)
    ctx.add_menu_item('Spectrum', 'spectrum_analyzer.home')
    # high queue: the orchestrator must not occupy the default workers its
    # per-album child jobs run on
    ctx.add_task('scan_changed', jobs.scan_changed_task, queue='high')
    # task name kept from the old orphan-recovery cron (existing enabled rows
    # resolve by name); the function is now the claim reconciler, which never
    # re-queues
    ctx.add_task('recover_deep_pending', jobs.reconcile_deep_pending_task,
                 queue='high')
    ctx.on_song_analyzed(jobs.on_song_analyzed)
