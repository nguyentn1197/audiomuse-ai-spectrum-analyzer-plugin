"""SpectrumAnalyzer: spectrum / fake-detection analysis for the whole library.

Pages:  /plugins/spectrum_analyzer/            album-grouped overview + scan buttons
        /plugins/spectrum_analyzer/album?...   per-track results with spectrograms
Worker: library scan (album by album), single-track re-run, on_song_analyzed hook.
Cleanup: results table cascades on DELETE FROM score (the core cleanup task).
"""

import html
import uuid
from urllib.parse import urlencode

from flask import Blueprint, request, redirect, url_for

from plugin.api import (
    get_db, get_setting, set_setting, table, render_page, manage_plugins_url,
    enqueue, save_task_status, TASK_STATUS_PENDING,
)

from . import jobs

bp = Blueprint('spectrum_analyzer', __name__)

VERDICT_STYLE = {
    'CLEAN':            ('#16a34a', 'Full bandwidth'),
    'CONSISTENT_LOSSY': ('#2563eb', 'Lossy, consistent with container'),
    'LOWPASSED':        ('#d97706', 'Low-passed (possibly genuine master)'),
    'UPSAMPLED':        ('#7c3aed', 'Fake hi-res: resampled from a lower-rate source'),
    'UPSCALED':         ('#7c3aed', 'Fake 24-bit: zero-padded 16-bit source'),
    'TRANSCODED_LOSSY': ('#dc2626', 'Lossy re-encoded from lower bitrate'),
    'FAKE_SUSPECT':     ('#dc2626', 'Suspect fake lossless (transcode)'),
}
SUSPECT_VERDICTS = ('FAKE_SUSPECT', 'TRANSCODED_LOSSY', 'UPSAMPLED', 'UPSCALED')


def migrate(db):
    cur = db.cursor()
    tbl = table('results')
    cur.execute(
        'CREATE TABLE IF NOT EXISTS ' + tbl + ' ('
        ' item_id TEXT PRIMARY KEY REFERENCES score (item_id) ON DELETE CASCADE,'
        ' title TEXT, artist TEXT, album TEXT, album_id TEXT,'
        ' file_path TEXT, suffix TEXT, bitrate INTEGER,'
        ' meta_fp TEXT, audio_md5 TEXT,'
        ' sample_rate INTEGER, seg_offset REAL, seg_seconds REAL,'
        ' cutoff_hz REAL, edge_db_khz REAL, shelf_db REAL,'
        ' verdict TEXT, est_source TEXT, confidence REAL, details TEXT,'
        ' container_bits INTEGER, effective_bits INTEGER,'
        ' spectrogram_b64 TEXT,'
        ' analyzed_at TIMESTAMP DEFAULT now())'
    )
    # 0.4.0: bit-depth columns for installs upgrading from older schemas
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS container_bits INTEGER')
    cur.execute('ALTER TABLE ' + tbl + ' ADD COLUMN IF NOT EXISTS effective_bits INTEGER')
    # 0.5.0: manual verification flag (verified tracks don't count as suspect)
    cur.execute('ALTER TABLE ' + tbl +
                ' ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE')
    cur.execute('CREATE INDEX IF NOT EXISTS ' + tbl + '_album_idx ON ' + tbl + ' (album)')
    # optional weekly incremental scan, shipped disabled (admin enables it)
    cur.execute(
        'INSERT INTO cron (name, task_type, cron_expr, enabled) VALUES (%s, %s, %s, FALSE) '
        'ON CONFLICT (task_type) DO NOTHING',
        ('plugin.spectrum_analyzer.scan_changed',
         'plugin.spectrum_analyzer.scan_changed', '0 4 * * 0'),
    )
    db.commit()
    cur.close()


def _esc(v):
    return html.escape('' if v is None else str(v))


def _badge(verdict, confidence=None):
    color, label = VERDICT_STYLE.get(verdict, ('#6b7280', verdict or '?'))
    conf = f' {int(confidence * 100)}%' if confidence is not None else ''
    return (f'<span title="{_esc(label)}" style="background:{color};color:#fff;'
            f'border-radius:4px;padding:.1rem .45rem;font-size:.8rem;'
            f'white-space:nowrap;">{_esc(verdict)}{conf}</span>')


def _khz(hz):
    return f'{hz / 1000.0:.1f}' if hz is not None else '?'


# ---------------------------------------------------------------- overview --

@bp.route('/')
def home():
    tbl = table('results')
    q = (request.args.get('q') or '').strip()
    page = max(0, int(request.args.get('p', 0) or 0))
    suspects_only = bool(request.args.get('suspects'))
    verified_only = bool(request.args.get('verified'))
    try:
        min_bad = max(0, int(request.args.get('min_bad', 0) or 0))
    except ValueError:
        min_bad = 0
    if suspects_only:
        min_bad = max(1, min_bad)
    per_page = 100

    # manually verified tracks never count as suspect
    bad_expr = 'COUNT(*) FILTER (WHERE verdict IN %s AND NOT verified)'

    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT COUNT(*), ' + bad_expr + ','
        ' COUNT(*) FILTER (WHERE verified),'
        ' COUNT(DISTINCT album) FROM ' + tbl, (SUSPECT_VERDICTS,))
    total, suspects, n_verified, n_albums = cur.fetchone()

    having = []
    having_params = []
    if min_bad:
        having.append(bad_expr + ' >= %s')
        having_params += [SUSPECT_VERDICTS, min_bad]
    if verified_only:
        having.append('COUNT(*) FILTER (WHERE verified) > 0')
    having_sql = (' HAVING ' + ' AND '.join(having)) if having else ''

    params = ([SUSPECT_VERDICTS, f'%{q}%'] + having_params
              + [SUSPECT_VERDICTS, per_page + 1, page * per_page])
    cur.execute(
        'SELECT album, COUNT(*), ' + bad_expr + ','
        ' COUNT(*) FILTER (WHERE verified),'
        ' MIN(cutoff_hz), to_char(MAX(analyzed_at), \'DD-MM-YYYY HH24:MI\')'
        ' FROM ' + tbl + ' WHERE album ILIKE %s GROUP BY album'
        + having_sql +
        ' ORDER BY ' + bad_expr + ' DESC, album'
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
        ))

    album_rows = ''.join(
        '<tr>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;">'
        f'<a href="{_esc(url_for("spectrum_analyzer.album", name=album or ""))}">{_esc(album) or "(no album)"}</a></td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{count}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:{"#dc2626" if bad else "#16a34a"};font-weight:600;">{bad}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:#6b7280;">{ver or ""}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{_khz(min_cut)} kHz</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;">{_esc(last)}</td>'
        '</tr>'
        for album, count, bad, ver, min_cut, last in rows)

    def page_url(p):
        params = {'q': q, 'min_bad': min_bad, 'p': p}
        if suspects_only:
            params['suspects'] = 1
        if verified_only:
            params['verified'] = 1
        return '?' + _esc(urlencode(params))

    nav = ''
    if page > 0:
        nav += f'<a href="{page_url(page - 1)}" style="margin-right:1rem;">&laquo; prev</a>'
    if has_next:
        nav += f'<a href="{page_url(page + 1)}">next &raquo;</a>'

    body = (
        f'<p><strong>{total}</strong> songs analyzed across <strong>{n_albums}</strong> albums; '
        f'<strong style="color:#dc2626;">{suspects}</strong> suspect '
        f'(fake lossless, transcode or fake hi-res), '
        f'<strong style="color:#6b7280;">{n_verified}</strong> manually verified '
        f'(excluded from suspect counts).</p>'
        f'<div style="margin:.8rem 0;">{scan_buttons}</div>'
        '<p style="font-size:.85rem;color:#6b7280;">Scans run on the worker, album by album; '
        'progress appears under Active Tasks. Already-analyzed tracks are skipped unless '
        'their file changed.</p>'
        f'<form method="get" style="margin:.8rem 0;display:flex;gap:.8rem;'
        f'align-items:center;flex-wrap:wrap;">'
        f'<input name="q" value="{_esc(q)}" placeholder="filter albums...">'
        f'<label style="white-space:nowrap;"><input type="checkbox" name="suspects" '
        f'value="1" {"checked" if suspects_only else ""}> only albums with suspects</label>'
        f'<label style="white-space:nowrap;">min suspect tracks '
        f'<input type="number" name="min_bad" min="0" value="{min_bad}" '
        f'style="width:4rem;"></label>'
        f'<label style="white-space:nowrap;"><input type="checkbox" name="verified" '
        f'value="1" {"checked" if verified_only else ""}> only albums with verified tracks</label>'
        f'<button type="submit" class="btn">Filter</button></form>'
        '<table style="border-collapse:collapse;width:100%;font-size:.95rem;">'
        '<tr><th style="text-align:left;padding:.3rem .6rem;">Album</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Tracks</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Suspect</th>'
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
    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT item_id, title, artist, suffix, bitrate, sample_rate, cutoff_hz,'
        ' edge_db_khz, verdict, est_source, confidence, details,'
        ' to_char(analyzed_at, \'DD-MM-YYYY HH24:MI\'), spectrogram_b64, album_id,'
        ' container_bits, effective_bits, verified'
        ' FROM ' + tbl + ' WHERE album = %s ORDER BY title', (name,))
    rows = cur.fetchall()
    cur.close()

    album_id = next((r[14] for r in rows if r[14]), None)

    cards = []
    for (item_id, title, artist, suffix, bitrate, sr, cutoff, edge, verdict,
         est, conf, details, when, png, _aid, cbits, ebits, verified) in rows:
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
            f'<div><strong>{_esc(title)}</strong> <span style="color:#6b7280;">'
            f'{_esc(artist)} &middot; {fmt}</span></div>'
            f'<div>{_badge(verdict, conf)}'
            f'<form method="post" action="{url_for("spectrum_analyzer.verify", item_id=item_id)}" '
            f'style="display:inline;margin-left:.6rem;">'
            f'<label style="white-space:nowrap;font-size:.85rem;" '
            f'title="Mark as manually checked: verified tracks are excluded from suspect counts">'
            f'<input type="checkbox" name="verified" value="1" '
            f'{"checked" if verified else ""} onchange="this.form.submit()"> '
            'verified</label></form>'
            f'<form method="post" action="{url_for("spectrum_analyzer.rescan", item_id=item_id)}" '
            f'style="display:inline;margin-left:.6rem;">'
            f'<button type="submit" class="btn" title="Force re-download and re-analyze this song">'
            'Re-analyze</button></form>'
            f'<form method="post" action="{url_for("spectrum_analyzer.deep_rescan", item_id=item_id)}" '
            f'style="display:inline;margin-left:.4rem;">'
            f'<button type="submit" class="btn" title="Analyze the ENTIRE file (not a segment) '
            f'and track the spectral edge over time: an edge that follows the music means a '
            f'genuine dark master, a constant wall means a resampler/encoder">'
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
        'Re-analyze album</button></form>') if rows else ''

    body = (
        f'<p><a href="{url_for("spectrum_analyzer.home")}">&laquo; all albums</a></p>'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'flex-wrap:wrap;gap:.5rem;">'
        f'<h3 style="margin:.2rem 0;">{_esc(name) or "(no album)"}</h3>{rescan_all}</div>'
        + (''.join(cards) or '<p>No analyzed tracks in this album.</p>')
    )
    return render_page(body, title='Spectrum Analyzer')


# ---------------------------------------------------------------- actions --

@bp.route('/scan', methods=['POST'])
def scan():
    mode = request.form.get('mode', 'changed')
    if mode not in ('changed', 'verify', 'force'):
        mode = 'changed'
    task_id = str(uuid.uuid4())
    save_task_status(task_id, jobs.TASK_TYPE, TASK_STATUS_PENDING,
                     details={'mode': mode, 'info': 'queued'})
    # the orchestrator lives on the high queue so its per-album children
    # (default queue) can be picked up by every default worker in parallel
    enqueue(jobs.scan_library_job, mode=mode, task_id=task_id, queue='high')
    return render_page(
        f'<p>Library scan started (mode: <strong>{_esc(mode)}</strong>). '
        'Albums are dispatched as parallel worker tasks; follow overall progress '
        'and per-album sub-tasks under Active Tasks.</p>'
        f'<p><a href="{url_for("spectrum_analyzer.home")}">Back</a></p>',
        title='Spectrum Analyzer')


@bp.route('/rescan/<item_id>', methods=['POST'])
def rescan(item_id):
    enqueue(jobs.analyze_track_job, item_id, queue='high')
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


@bp.route('/deep_rescan/<item_id>', methods=['POST'])
def deep_rescan(item_id):
    enqueue(jobs.analyze_track_job, item_id, deep=True, queue='high')
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


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
        task_id = str(uuid.uuid4())
        save_task_status(task_id, jobs.ALBUM_TASK_TYPE, TASK_STATUS_PENDING,
                         sub_type_identifier=name,
                         details={'mode': 'force', 'album': name, 'info': 'queued'})
        enqueue(jobs.scan_album_job, album_id, name, mode='force', task_id=task_id)
    else:
        # no album id stored (e.g. hook-inserted rows): re-run track by track
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT item_id FROM ' + table('results') + ' WHERE album = %s',
                    (name,))
        item_ids = [r[0] for r in cur.fetchall()]
        cur.close()
        for item_id in item_ids:
            enqueue(jobs.analyze_track_job, item_id)
    return redirect(request.referrer
                    or url_for('spectrum_analyzer.album', name=name))


# --------------------------------------------------------------- settings --

@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        for key, lo, hi, dflt in (('segment_seconds', 20, 600, 90),
                                  ('drop_db', 20, 70, 40),
                                  ('img_w', 400, 2000, 800),
                                  ('img_h', 160, 800, 280)):
            try:
                set_setting(key, max(lo, min(hi, int(request.form.get(key, dflt)))))
            except (TypeError, ValueError):
                set_setting(key, dflt)
        set_setting('hook_enabled', bool(request.form.get('hook_enabled')))
        return redirect(manage_plugins_url())

    def num(key, dflt, label, hint):
        return (f'<label style="display:block;margin:.6rem 0;">{label} '
                f'<input type="number" name="{key}" value="{get_setting(key, dflt)}" '
                f'style="width:6rem;"> <span style="color:#6b7280;font-size:.85rem;">'
                f'{hint}</span></label>')

    hook = 'checked' if get_setting('hook_enabled', True) else ''
    body = (
        '<form method="post">'
        + num('segment_seconds', 90, 'Analyzed segment (s)',
              'taken from the middle of each track')
        + num('drop_db', 40, 'Cutoff threshold (dB)',
              'level drop below the 1-8 kHz reference that counts as "no content"')
        + num('img_w', 800, 'Spectrogram width (px)', '')
        + num('img_h', 280, 'Spectrogram height (px)',
              'bigger images mean more base64 in the database')
        + f'<label style="display:block;margin:.6rem 0;">'
          f'<input type="checkbox" name="hook_enabled" {hook}> '
          'Also analyze songs automatically during core analysis (on_song_analyzed hook)</label>'
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
    ctx.on_song_analyzed(jobs.on_song_analyzed)
