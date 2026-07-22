#!/usr/bin/env python3
"""Windows debug app for manual verification of the SpectrumAnalyzer DSP
pipeline before cutting a plugin release.

Runs `plugins/SpectrumAnalyzer/dsp.py` completely unmodified, exactly like the
test harness does: `tests/librosa_stub.py` stands in for librosa, so the real
production analysis code executes end to end (cutoff, edge, shelf, bit depth,
codec probe, verdict, spectrogram).

- Drag files or folders onto the window (folders are walked recursively);
  only suffixes the DSP supports are imported.
- Click a file, then "Analyze (segment)" or "Deep analyze".
- Scans run in a process pool (one worker per CPU core) so the UI stays
  responsive; the detail pane refreshes itself when its scan finishes.
- Results are written as JSON to a temp folder that is deleted on exit --
  no persistent storage.

Usage: py debug_app\\spectrum_debug_app.py   (see debug_app/README.md)
"""
import atexit
import hashlib
import json
import os
import queue
import shutil
import sys
import tempfile
import tkinter as tk
from concurrent.futures import ProcessPoolExecutor
from tkinter import filedialog, messagebox, ttk

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_dsp():
    """Import the real dsp module with the test suite's librosa stand-in.
    Used identically in the GUI process and in every pool worker."""
    for p in (os.path.join(REPO_ROOT, 'tests'),
              os.path.join(REPO_ROOT, 'plugins', 'SpectrumAnalyzer')):
        if p not in sys.path:
            sys.path.insert(0, p)
    import librosa_stub  # noqa: F401  (installs sys.modules['librosa'])
    import dsp
    return dsp


# ---------------------------------------------------------------- worker side

_worker_dsp = None


def _worker_init():
    global _worker_dsp
    _worker_dsp = _import_dsp()


def _scan_worker(path, suffix, bitrate_kbps, deep):
    return _worker_dsp.analyze_file(path, suffix=suffix,
                                    bitrate_kbps=bitrate_kbps, deep=deep)


# ------------------------------------------------------------------- GUI side

# Mirrors VERDICT_STYLE in plugins/SpectrumAnalyzer/__init__.py -- copied, not
# imported, because importing the blueprint would pull in Flask/DB code.
VERDICT_STYLE = {
    'CLEAN':            ('#16a34a', 'Full bandwidth'),
    'CONSISTENT_LOSSY': ('#2563eb', 'Lossy, consistent with container'),
    'LOWPASSED':        ('#d97706', 'Low-passed (possibly genuine master)'),
    'UPSAMPLED':        ('#7c3aed', 'Fake hi-res: resampled from a lower-rate source'),
    'UPSCALED':         ('#7c3aed', 'Fake 24-bit: zero-padded 16-bit source'),
    'TRANSCODED_LOSSY': ('#dc2626', 'Lossy re-encoded from lower bitrate'),
    'FAKE_SUSPECT':     ('#dc2626', 'Suspect fake lossless (transcode)'),
    'INCONCLUSIVE':     ('#6b7280', 'Could not be analyzed (decode failure or unsupported format)'),
}


def _estimate_kbps(path):
    """Rough declared-bitrate stand-in (the plugin gets this from the media
    server): container size over soundfile duration. None when soundfile
    can't open the file (e.g. m4a) -- type a value manually then."""
    try:
        import soundfile as sf
        info = sf.info(path)
        dur = info.frames / float(info.samplerate)
        if dur > 0:
            return int(round(os.path.getsize(path) * 8 / dur / 1000))
    except Exception:
        pass
    return None


class DebugApp:
    POLL_MS = 150

    def __init__(self, root, dsp_mod):
        self.root = root
        self.dsp = dsp_mod
        self.supported = sorted(dsp_mod.LOSSLESS_SUFFIXES | dsp_mod.LOSSY_SUFFIXES)
        self.tmp_dir = tempfile.mkdtemp(prefix='spectrum_analyzer_debug_')
        atexit.register(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self.pool = ProcessPoolExecutor(max_workers=os.cpu_count() or 2,
                                        initializer=_worker_init)
        self.done_queue = queue.Queue()
        self.files = {}  # path -> {'status', 'mode', 'result_file', 'error', 'kbps'}

        root.title('SpectrumAnalyzer DSP debug')
        root.geometry('1280x800')
        self._build_ui()
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.after(self.POLL_MS, self._poll_done)

    # ---------------------------------------------------------------- layout

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=4)
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text='Add files…', command=self._add_files_dialog).pack(side='left')
        ttk.Button(toolbar, text='Add folder…', command=self._add_folder_dialog).pack(side='left', padx=4)
        ttk.Button(toolbar, text='Scan all pending (segment)',
                   command=self._scan_all_pending).pack(side='left', padx=4)
        ttk.Button(toolbar, text='Clear list', command=self._clear_list).pack(side='left', padx=4)
        self.drop_hint = ttk.Label(
            toolbar, foreground='#6b7280',
            text='Drop audio files or folders anywhere in this window '
                 f'({", ".join(self.supported)})')
        self.drop_hint.pack(side='left', padx=12)

        paned = ttk.Panedwindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True)

        left = ttk.Frame(paned)
        self.tree = ttk.Treeview(left, columns=('status', 'verdict'), selectmode='browse')
        self.tree.heading('#0', text='File')
        self.tree.heading('status', text='Status')
        self.tree.heading('verdict', text='Verdict')
        self.tree.column('#0', width=380)
        self.tree.column('status', width=110)
        self.tree.column('verdict', width=140)
        ysb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        ysb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self._show_selected())
        paned.add(left, weight=1)

        # Detail pane: scrollable (spectrogram + JSON dump is tall).
        right = ttk.Frame(paned)
        self.canvas = tk.Canvas(right, highlightthickness=0)
        vsb = ttk.Scrollbar(right, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.detail = ttk.Frame(self.canvas, padding=8)
        self._detail_win = self.canvas.create_window((0, 0), window=self.detail, anchor='nw')
        self.detail.bind('<Configure>',
                         lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',
                         lambda e: self.canvas.itemconfigure(self._detail_win, width=e.width))
        self.canvas.bind_all('<MouseWheel>',
                             lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), 'units'))
        paned.add(right, weight=2)

        self._show_placeholder('Import files, then select one.')

    # ------------------------------------------------------------- importing

    def _add_files_dialog(self):
        exts = ' '.join(f'*.{s}' for s in self.supported)
        paths = filedialog.askopenfilenames(filetypes=[('Audio', exts), ('All files', '*.*')])
        self._import_paths(paths)

    def _add_folder_dialog(self):
        folder = filedialog.askdirectory()
        if folder:
            self._import_paths([folder])

    def _on_drop(self, event):
        self._import_paths(self.root.tk.splitlist(event.data))

    def _import_paths(self, paths):
        added = skipped = 0
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                for base, _dirs, names in os.walk(p):
                    for name in sorted(names):
                        a, s = self._import_file(os.path.join(base, name))
                        added, skipped = added + a, skipped + s
            else:
                a, s = self._import_file(p)
                added, skipped = added + a, skipped + s
        if skipped and not added:
            self.drop_hint.configure(text=f'{skipped} file(s) skipped: unsupported suffix '
                                          f'(supported: {", ".join(self.supported)})')
        elif added:
            self.drop_hint.configure(text=f'{added} file(s) imported'
                                          + (f', {skipped} unsupported skipped' if skipped else ''))

    def _import_file(self, path):
        """Returns (added, skipped) as 0/1 counts."""
        suffix = os.path.splitext(path)[1].lstrip('.').lower()
        if suffix not in self.supported:
            return 0, 1
        if path in self.files:
            return 0, 0
        self.files[path] = {'status': 'imported', 'mode': None, 'result_file': None,
                            'error': None, 'kbps': _estimate_kbps(path)}
        self.tree.insert('', 'end', iid=path, text=os.path.basename(path),
                         values=('imported', ''))
        return 1, 0

    def _clear_list(self):
        running = [p for p, f in self.files.items() if f['status'] == 'scanning']
        if running:
            messagebox.showinfo('Scans running',
                                f'{len(running)} scan(s) still running -- wait for them first.')
            return
        self.files.clear()
        self.tree.delete(*self.tree.get_children())
        self._show_placeholder('Import files, then select one.')

    # -------------------------------------------------------------- scanning

    def _scan(self, path, deep):
        entry = self.files[path]
        if entry['status'] == 'scanning':
            return
        try:
            kbps = int(self.kbps_var.get()) if self.kbps_var.get().strip() else None
        except (ValueError, AttributeError):
            kbps = entry['kbps']
        entry.update(status='scanning', mode='deep' if deep else 'segment',
                     error=None, kbps=kbps)
        self.tree.set(path, 'status', 'scanning…')
        suffix = os.path.splitext(path)[1].lstrip('.').lower()
        fut = self.pool.submit(_scan_worker, path, suffix, kbps, deep)
        fut.add_done_callback(lambda f, p=path: self.done_queue.put((p, f)))
        self._show_detail(path)

    def _scan_all_pending(self):
        for path, entry in self.files.items():
            if entry['status'] == 'imported':
                entry.update(status='scanning', mode='segment', error=None)
                self.tree.set(path, 'status', 'scanning…')
                suffix = os.path.splitext(path)[1].lstrip('.').lower()
                fut = self.pool.submit(_scan_worker, path, suffix, entry['kbps'], False)
                fut.add_done_callback(lambda f, p=path: self.done_queue.put((p, f)))

    def _poll_done(self):
        try:
            while True:
                path, fut = self.done_queue.get_nowait()
                entry = self.files.get(path)
                if entry is None:  # cleared while running
                    continue
                try:
                    result = fut.result()
                except Exception as exc:  # worker crash, not a dsp verdict
                    entry.update(status='error', error=str(exc))
                    self.tree.set(path, 'status', 'error')
                    self.tree.set(path, 'verdict', '')
                else:
                    rf = os.path.join(self.tmp_dir,
                                      hashlib.sha1(path.encode()).hexdigest() + '.json')
                    with open(rf, 'w', encoding='utf-8') as f:
                        json.dump(result, f)
                    entry.update(status='done', result_file=rf)
                    self.tree.set(path, 'status', f'done ({entry["mode"]})')
                    self.tree.set(path, 'verdict', result['verdict'] or '?')
                if self._selected_path() == path:
                    self._show_detail(path)  # auto-refresh the open page
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll_done)

    # ------------------------------------------------------------ detail pane

    def _selected_path(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _show_selected(self):
        path = self._selected_path()
        if path:
            self._show_detail(path)

    def _clear_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        self.canvas.yview_moveto(0)

    def _show_placeholder(self, text):
        self._clear_detail()
        ttk.Label(self.detail, text=text, foreground='#6b7280').pack(anchor='w')

    def _show_detail(self, path):
        self._clear_detail()
        entry = self.files[path]

        ttk.Label(self.detail, text=os.path.basename(path),
                  font=('', 12, 'bold')).pack(anchor='w')
        ttk.Label(self.detail, text=path, foreground='#6b7280',
                  wraplength=700).pack(anchor='w')

        controls = ttk.Frame(self.detail)
        controls.pack(anchor='w', pady=6)
        ttk.Label(controls, text='Declared kbps:').pack(side='left')
        self.kbps_var = tk.StringVar(value='' if entry['kbps'] is None else str(entry['kbps']))
        ttk.Entry(controls, textvariable=self.kbps_var, width=7).pack(side='left', padx=(2, 10))
        scanning = entry['status'] == 'scanning'
        state = 'disabled' if scanning else 'normal'
        ttk.Button(controls, text='Analyze (segment)', state=state,
                   command=lambda: self._scan(path, deep=False)).pack(side='left')
        ttk.Button(controls, text='Deep analyze', state=state,
                   command=lambda: self._scan(path, deep=True)).pack(side='left', padx=4)

        if scanning:
            ttk.Label(self.detail, text=f'Scanning ({entry["mode"]})… '
                                        'this pane refreshes itself when done.',
                      foreground='#d97706').pack(anchor='w', pady=4)
            return
        if entry['status'] == 'error':
            ttk.Label(self.detail, text=f'Worker error: {entry["error"]}',
                      foreground='#dc2626', wraplength=700).pack(anchor='w', pady=4)
            return
        if not entry['result_file']:
            ttk.Label(self.detail, text='Not analyzed yet.',
                      foreground='#6b7280').pack(anchor='w', pady=4)
            return

        with open(entry['result_file'], encoding='utf-8') as f:
            r = json.load(f)
        self._render_result(r, entry['mode'])

    def _render_result(self, r, mode):
        # Same information layout as the plugin's album-page track card.
        color, label = VERDICT_STYLE.get(r['verdict'], ('#6b7280', r['verdict'] or '?'))
        badge_row = ttk.Frame(self.detail)
        badge_row.pack(anchor='w', pady=(6, 2))
        conf = f' {round(r["confidence"] * 100)}%' if r.get('confidence') is not None else ''
        tk.Label(badge_row, text=f' {r["verdict"]}{conf} ', bg=color, fg='white',
                 font=('', 10, 'bold')).pack(side='left')
        ttk.Label(badge_row, text=f'  {label}  ·  {mode} mode').pack(side='left')

        cutoff = r.get('cutoff_hz')
        summary = (
            f'Cutoff {cutoff / 1000:.1f} kHz' if cutoff else 'Cutoff ?'
        ) + (
            f'  ·  edge {r["edge_db_khz"]} dB/kHz' if r.get('edge_db_khz') is not None
            else '  ·  edge ?'
        ) + (
            f'  ·  shelf {r["shelf_db"]} dB' if r.get('shelf_db') is not None else ''
        ) + (
            f'  ·  {r["effective_bits"]}/{r["container_bits"]} bit'
            if r.get('container_bits') else ''
        ) + (
            f'  ·  {r["sample_rate"]} Hz' if r.get('sample_rate') else ''
        )
        ttk.Label(self.detail, text=summary).pack(anchor='w')
        ttk.Label(self.detail, text=f'Estimated source: {r.get("est_source") or "?"}',
                  foreground='#6b7280', wraplength=700).pack(anchor='w')

        if r.get('spectrogram_b64'):
            img = tk.PhotoImage(data=r['spectrogram_b64'])  # Tk 8.6 reads base64 PNG
            img_label = ttk.Label(self.detail, image=img)
            img_label.image = img  # keep a reference or Tk garbage-collects it
            img_label.pack(anchor='w', pady=6)
        else:
            ttk.Label(self.detail, text='(no spectrogram rendered)',
                      foreground='#6b7280').pack(anchor='w', pady=6)

        ttk.Label(self.detail, text='Raw metrics', font=('', 10, 'bold')).pack(anchor='w')
        details = dict(r)
        details['spectrogram_b64'] = ('<png, %d chars>' % len(r['spectrogram_b64'])
                                      if r.get('spectrogram_b64') else None)
        try:
            details['details'] = json.loads(r['details'])
        except (TypeError, ValueError):
            pass
        text = tk.Text(self.detail, height=32, width=100, wrap='word',
                       font=('Consolas', 9))
        text.insert('1.0', json.dumps(details, indent=2))
        text.configure(state='disabled')
        text.pack(anchor='w', fill='x', pady=(2, 12))

    # ---------------------------------------------------------------- closing

    def _on_close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.root.destroy()


def main():
    dsp_mod = _import_dsp()
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd = True
    except ImportError:
        root = tk.Tk()
        dnd = False

    app = DebugApp(root, dsp_mod)
    if dnd:
        root.drop_target_register(DND_FILES)
        root.dnd_bind('<<Drop>>', app._on_drop)
    else:
        app.drop_hint.configure(
            text='tkinterdnd2 not installed -- drag & drop disabled, '
                 'use the Add buttons (pip install tkinterdnd2)')
    root.mainloop()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
