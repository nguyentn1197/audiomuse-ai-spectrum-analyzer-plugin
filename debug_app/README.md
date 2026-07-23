# DSP debug app (Windows)

A throwaway desktop UI for manually verifying `plugins/SpectrumAnalyzer/dsp.py`
on local files before cutting a plugin release — no AudioMuse instance needed.
It runs the production `dsp.py` **unmodified**, using `tests/librosa_stub.py`
as the librosa stand-in exactly like the test suite does.

## Setup (once)

```bat
py -m pip install numpy soundfile matplotlib tkinterdnd2
```

`tkinterdnd2` is only for drag & drop — without it the app still works via the
"Add files/folder" buttons. For m4a/mp4/opus-in-ogg/wma/mpc files the DSP
shells out to `ffmpeg`/`ffprobe`, so put a real ffmpeg on `PATH` if you want
those analyzed (without it they come back INCONCLUSIVE, same as in production).

## Run

```bat
py debug_app\spectrum_debug_app.py
```

- Drop audio files or folders onto the window (folders are walked
  recursively); only DSP-supported suffixes are imported, the rest are
  skipped with a note.
- Select a file → "Analyze (segment)" or "Deep analyze". "Declared kbps" is
  prefilled with a size/duration estimate (the plugin normally gets this from
  the media server) and can be edited before scanning — it feeds the
  transcode gate.
- "Scan all pending (segment)" fans every not-yet-scanned file out over one
  worker process per CPU core; the UI never blocks, rows show "scanning…",
  and an open detail pane refreshes itself when its scan completes.
- The detail pane mirrors the plugin's track card: colored verdict badge with
  confidence, cutoff/edge/shelf/bit-depth summary, estimated source, inline
  spectrogram, and the full raw result (with `details` pretty-printed).

Results are stored as JSON in a temp folder and deleted when the app closes —
nothing persists between runs.
