"""SpectrumAnalyzer: spectrum / fake-detection analysis for the whole library.

Pages:  /plugins/spectrum_analyzer/            album-grouped overview + scan buttons
        /plugins/spectrum_analyzer/album?...   per-track results with spectrograms
Worker: library scan (album by album), single-track re-run, on_song_analyzed hook.
Cleanup: results table cascades on DELETE FROM score (the core cleanup task).
"""

import html
import uuid

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
    'TRANSCODED_LOSSY': ('#dc2626', 'Lossy re-encoded from lower bitrate'),
    'FAKE_SUSPECT':     ('#dc2626', 'Suspect fake lossless (transcode)'),
}
SUSPECT_VERDICTS = ('FAKE_SUSPECT', 'TRANSCODED_LOSSY')


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
        ' spectrogram_b64 TEXT,'
        ' analyzed_at TIMESTAMP DEFAULT now())'
    )
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
    per_page = 100

    db = get_db()
    cur = db.cursor()
    cur.execute(
        'SELECT COUNT(*),'
        ' COUNT(*) FILTER (WHERE verdict IN %s),'
        ' COUNT(DISTINCT album) FROM ' + tbl, (SUSPECT_VERDICTS,))
    total, suspects, n_albums = cur.fetchone()

    cur.execute(
        'SELECT album, COUNT(*),'
        ' COUNT(*) FILTER (WHERE verdict IN %s),'
        ' MIN(cutoff_hz), to_char(MAX(analyzed_at), \'DD-MM-YYYY HH24:MI\')'
        ' FROM ' + tbl + ' WHERE album ILIKE %s GROUP BY album'
        ' ORDER BY COUNT(*) FILTER (WHERE verdict IN %s) DESC, album'
        ' LIMIT %s OFFSET %s',
        (SUSPECT_VERDICTS, f'%{q}%', SUSPECT_VERDICTS, per_page + 1, page * per_page))
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
        f'<a href="{url_for("spectrum_analyzer.album")}?name={_esc(album)}">{_esc(album) or "(no album)"}</a></td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{count}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;'
        f'color:{"#dc2626" if bad else "#16a34a"};font-weight:600;">{bad}</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;text-align:right;">{_khz(min_cut)} kHz</td>'
        f'<td style="padding:.3rem .6rem;border-top:1px solid #ccc;">{_esc(last)}</td>'
        '</tr>'
        for album, count, bad, min_cut, last in rows)

    nav = ''
    if page > 0:
        nav += f'<a href="?q={_esc(q)}&p={page - 1}" style="margin-right:1rem;">&laquo; prev</a>'
    if has_next:
        nav += f'<a href="?q={_esc(q)}&p={page + 1}">next &raquo;</a>'

    body = (
        f'<p><strong>{total}</strong> songs analyzed across <strong>{n_albums}</strong> albums; '
        f'<strong style="color:#dc2626;">{suspects}</strong> suspect '
        f'(fake lossless or lossy transcode).</p>'
        f'<div style="margin:.8rem 0;">{scan_buttons}</div>'
        '<p style="font-size:.85rem;color:#6b7280;">Scans run on the worker, album by album; '
        'progress appears under Active Tasks. Already-analyzed tracks are skipped unless '
        'their file changed.</p>'
        f'<form method="get" style="margin:.8rem 0;">'
        f'<input name="q" value="{_esc(q)}" placeholder="filter albums...">'
        f'<button type="submit" class="btn">Filter</button></form>'
        '<table style="border-collapse:collapse;width:100%;font-size:.95rem;">'
        '<tr><th style="text-align:left;padding:.3rem .6rem;">Album</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Tracks</th>'
        '<th style="text-align:right;padding:.3rem .6rem;">Suspect</th>'
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
        ' to_char(analyzed_at, \'DD-MM-YYYY HH24:MI\'), spectrogram_b64'
        ' FROM ' + tbl + ' WHERE album = %s ORDER BY title', (name,))
    rows = cur.fetchall()
    cur.close()

    cards = []
    for (item_id, title, artist, suffix, bitrate, sr, cutoff, edge, verdict,
         est, conf, details, when, png) in rows:
        fmt = f'{_esc(suffix)}{f" {bitrate}k" if bitrate else ""} / {sr or "?"} Hz'
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
            f'<form method="post" action="{url_for("spectrum_analyzer.rescan", item_id=item_id)}" '
            f'style="display:inline;margin-left:.6rem;">'
            f'<button type="submit" class="btn" title="Force re-download and re-analyze this song">'
            'Re-analyze</button></form></div></div>'
            f'<p style="margin:.4rem 0;font-size:.9rem;">Cutoff <strong>{_khz(cutoff)} kHz</strong>'
            f' &middot; edge {edge if edge is not None else "?"} dB/kHz'
            f' &middot; {_esc(est)} &middot; analyzed {_esc(when)}</p>'
            f'{img}'
            f'<details style="margin-top:.4rem;font-size:.8rem;color:#6b7280;">'
            f'<summary>raw metrics</summary><pre style="white-space:pre-wrap;">{_esc(details)}</pre>'
            '</details></div>'
        )

    body = (
        f'<p><a href="{url_for("spectrum_analyzer.home")}">&laquo; all albums</a></p>'
        f'<h3>{_esc(name) or "(no album)"}</h3>'
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
    enqueue(jobs.scan_library_job, mode=mode, task_id=task_id)
    return render_page(
        f'<p>Library scan started (mode: <strong>{_esc(mode)}</strong>). '
        'Follow progress under Active Tasks.</p>'
        f'<p><a href="{url_for("spectrum_analyzer.home")}">Back</a></p>',
        title='Spectrum Analyzer')


@bp.route('/rescan/<item_id>', methods=['POST'])
def rescan(item_id):
    enqueue(jobs.analyze_track_job, item_id, queue='high')
    return redirect(request.referrer or url_for('spectrum_analyzer.home'))


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
    ctx.add_task('scan_changed', jobs.scan_changed_task)
    ctx.on_song_analyzed(jobs.on_song_analyzed)
