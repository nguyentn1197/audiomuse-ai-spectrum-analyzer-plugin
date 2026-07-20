# Forensic-analysis improvement plan

*Revised 2026-07-20 after a second review round: codec-aware transcode gating
moved into Phase 1 (the original rejection was wrong — the generic bitrate
table does change verdicts), basic DSD support and evidence-only ultrasonic
band analysis added to Phase 2, and the bit-depth / cutoff-width / MD5
bullets corrected.*

*Revised again 2026-07-20 after a third round. Six real corrections adopted:
(1) an analysis-revision stamp so unchanged tracks actually receive improved
analysis, (2) new statuses are substatuses under one neutral INCONCLUSIVE
verdict — never new primary verdicts, (3) integrity states are progressive
(a metadata probe can't say "VALID"), (4) sustained occupancy must feed the
deep verdict or max-hold laundering survives, (5) deep-scan chunks get a
sample budget so peak memory doesn't scale with sample rate, (6) the sampled
zero-padding result is labelled as sampled. ffmpeg presence is now
**verified** (AudioMuse Dockerfile installs it). The same round's process
apparatus — a separate Phase 0, benchmark acceptance matrices, holdout
corpora, per-flag status-enum schemas, dedicated concurrency pools — is
rejected as complexity this plugin doesn't need; see "Round-3 feedback
disposition" at the bottom.*

*Revised a third time 2026-07-20 after a fourth (reduction-focused) round.
Accepted: the revision check must cover the `changed`-mode fingerprint skip
too (verified — `jobs.py:504` skips before any download); deep-scan routing
gets an explicit `deep_eligible` flag instead of hanging off the primary
verdict (fixing a real contradiction: DSD files were INCONCLUSIVE *and*
needed the deep-mode DSD branch); integrity becomes status + coverage (a
capped long file is not a partial decode failure); the deep profile becomes
one global streaming percentile instead of occupancy-with-a-new-threshold —
a genuine simplification that lets `_find_cutoff`/`_verdict` run unchanged
on a corrected input; per-channel spectra become two-stage inside deep mode;
sampled zero-padding drops to 0.9 with full-file confirmation restoring
0.95. Phase 2 is cut hard: auto-escalation, splice detection, I/O
consolidation and the alias multi-window redesign move to DEFERRED; DSD
sampled decode and ultrasonic band evidence are marked optional. See
"Round-4 feedback disposition" at the bottom.*

*Final revision 2026-07-20 after a fifth round (convergence — mostly
precision fixes, no new scope). Adopted: DSD deep-eligibility keys on
*implemented analyzer capability*, not ffmpeg presence alone; the global
percentile is pinned down (all frames — preserving existing percentile
semantics — fixed documented histogram range/bucket, validated against
numpy on fixtures); compressed-format sampling starts at three ffmpeg
windows; alias hygiene shrinks to the energy-floor guard alone (detrending
joins the deferred redesign); truncation fixtures get per-mode
expectations; JSON facts get single owners; the delivery flag is renamed to
the observation (`codec_mismatch`) rather than one interpretation; the
per-channel stage-2 trigger is tightened. Pushed back on demoting
TRANSCODED_LOSSY to evidence-only — that would regress a shipped,
fixture-validated detector out of the suspect counts. The plan is now
considered implementation-ready; further review rounds are expected to hit
diminishing returns. See "Round-5 feedback disposition" at the bottom.*

*Round 6 (2026-07-20, terminal): six precision fixes, all adopted, none
adding scope — lazy ffprobe confirmation for the MP3 gate (resolving a real
fast-path contradiction), `integrity.coverage = partial`, all
frequency-domain widths in hertz (not just cutoff width), Phase-1 DSF
metadata limited to what ffprobe actually provides, the leftover "0.95
sampled" wording removed, and INCONCLUSIVE made visible in the UI (count +
filter). The round endorsed the TRANSCODED_LOSSY pushback with a sensible
diagnose-before-retuning discipline, adopted. **Plan frozen — per the
round's own recommendation, the next validation target is the Phase-1
implementation and its tests, not this document.***

Assessment of an external suggestion list for turning the plugin into a fuller
forensic audio analyzer. Each suggestion was checked against the actual code
(`plugins/SpectrumAnalyzer/dsp.py`, `jobs.py`). Items were kept only when the
underlying claim is true in this codebase **and** the benefit justifies the
cost inside an AudioMuse library-scan pipeline; the rest are listed at the
bottom with reasons.

Guiding scope: this plugin is a **fake-lossless / provenance detector that
runs unattended over thousands of tracks**. Improvements that raise verdict
reliability at similar cost come first; mastering-QC extras are opt-in (deep
mode); anything needing a new decoder stack or a calibration corpus we don't
have is deferred.

## Verified weaknesses driving this plan

- `_load_segment` analyzes a fixed 00:30 offset (docstring claims "middle"),
  mono only — one 90 s window per track (`dsp.py:36-51`).
- Verdict branch trusts the *supplied suffix*; `dsf`/`dff` are in neither
  suffix set. Worse: when ffmpeg is present, librosa's audioread fallback
  *silently* decodes DSD to ~352.8 kHz PCM and the PCM heuristics run on
  it — DSD noise shaping reads as full-bandwidth content, so even a
  lossy-sourced DSD transcode comes back **CLEAN**; without ffmpeg the
  decode just fails. Only if the cutoff somehow lands low does the
  unknown suffix fall into the lossy branch (`CONSISTENT_LOSSY`). Every
  one of these outcomes is wrong (`dsp.py:25-26`, `dsp.py:353-364`).
- Deep scan swallows decode exceptions and silently reports a partial result
  if ≥ 5 s decoded — truncation/corruption is never surfaced
  (`dsp.py:86-87`, `dsp.py:119`).
- Cutoff = highest run of **3 consecutive FFT bins** (~32 Hz at 44.1 kHz)
  above threshold — a single ultrasonic pilot tone or noise cluster can
  extend it (`dsp.py:125-137`).
- Deep scan combines per-chunk 95th-percentile profiles with `np.maximum` —
  one bright chunk can launder low-passed sections (`dsp.py:96`). (Partly
  mitigated already by the per-second edge series.)
- Bit-depth probe takes the *minimum* trailing-zero count: one full-depth
  sample makes the whole file look full-depth (`dsp.py:242-243`).
- Every track — clean or not — pays a Matplotlib PNG render at
  `compress_level=9` plus 100–300 KB of Postgres storage (`dsp.py:405`).
- `file_md5` re-reads the finished download; the bit-depth probe opens the
  file again; deep mode reopens per chunk (`jobs.py:392`, `dsp.py:231`).

---

## Phase 1 — verdict reliability at (roughly) current cost

- [ ] **Analysis-revision stamp (do this first).** The skip logic
      (`changed` = fingerprint match, `verify` = encoded-MD5 match,
      `jobs.py:394`) has no notion of *which analyzer* produced a stored
      result — so every improvement below would leave unchanged tracks on
      their old verdicts forever unless the user runs a force scan. Fix:
      store one `analysis_rev` string per result (a hand-bumped constant,
      folded with the few verdict-relevant settings like `drop_db`); the rev
      check must cover **every** skip path — `changed` mode skips on
      fingerprint match *before any download* (`jobs.py:504`), so it needs
      fingerprint-match **and** rev-match; `verify` mode needs MD5-match
      **and** rev-match. (Round 4 caught that the original wording covered
      only the MD5 path — with that omission, `changed`-mode cron scans
      would still have preserved old verdicts forever.) A rev bump
      intentionally causes one library-wide reanalysis. Deliberately *not*
      adopted from review: separate fixture revisions, per-setting
      signatures, calibration-table revisions — one string is enough.
      *Cost: one column + one comparison.*

- [ ] **Verdict/substatus contract.** New states introduced by this plan
      (integrity, DSD, unsupported formats) are **substatuses in the details
      JSON, never new primary verdicts** — `bad_expr`, the album filters and
      the bulk `deep_rescan_all` route are all keyed to the fixed verdict
      set (`__init__.py:201`), so new primary values would either vanish
      from the UI or pollute suspect counts and bulk deep-scan targeting.
      Add exactly one new primary verdict, `INCONCLUSIVE` (excluded from
      `bad_expr` but **never invisible** — round-6 product requirement:
      the UI shows an INCONCLUSIVE count and filter with the substatus
      reason, so a "clean scan" can't silently hide files that were never
      meaningfully analyzed; one count + one filter in the existing verdict
      UI, no new dashboard), for files where analysis could not
      meaningfully run:
      unsupported codec, DSD without ffmpeg, decode failure. Deep-scan
      routing must **not** hang off the primary verdict — that created a
      real contradiction (DSD files are INCONCLUSIVE *and* are exactly what
      the deep-mode DSD branch exists for). Store an explicit
      `deep_eligible` flag (plus a short reason), computed from the
      substatuses — and, per round 5, from **implemented capability**, not
      raw tool presence: DSD is eligible only when ffmpeg is available
      *and* the optional DSD sampled-validation milestone has actually
      shipped (ffmpeg installed proves decodability, not that the analyzer
      exists — until then `dsd.analysis_status = not_supported`,
      `deep_eligible = false`, so the UI never offers a deep scan that
      can't add anything). Unsupported codec or decode failure → not
      eligible. Everything finer lives in `integrity.*` / `dsd.*` /
      `quality.*`. *Cost: negligible runtime; small UI addition.*

- [ ] **Container/codec probe + progressive integrity states.**
      Probe order: magic bytes (catches DSF/DFF *before* any decode) →
      `soundfile.info` for native PCM formats → **ffprobe as the fallback**
      (verified: the AudioMuse Dockerfile installs ffmpeg; keep a runtime
      capability check for nonstandard installs). ffprobe is a subprocess —
      do **not** spawn it for every track: FLAC/WAV/plain MP3 stay on the
      in-process fast path; ffprobe runs only for multi-codec containers
      (m4a/mp4/ogg), magic-bytes-vs-metadata conflicts, or when a reliable
      stream bitrate is needed for the verdict-changing gate. Do *not*
      write a custom MP4 `ftyp`/`stsd` parser — ffprobe gives more
      information with zero maintenance. Feed the *detected* codec — not the filename — into
      `_verdict`. Bitrate for the transcode gate is resolved **lazily**
      (round-6 fix for a real contradiction: "plain MP3 skips ffprobe" and
      "the gate prefers a probed bitrate" couldn't both hold, since
      soundfile gives no authoritative stream bitrate): use the
      media-server `bitRate` as the preliminary value, spawn ffprobe *only*
      when that preliminary value would produce TRANSCODED_LOSSY, and
      confirm the verdict against the probed bitrate — so ffprobe runs on
      potentially-suspicious MP3s, not the whole MP3 library. If ffprobe
      is unavailable or the bitrate stays unreliable, the gate doesn't
      fire (evidence note instead; the declared value may be a container
      rate including artwork, a rounded VBR average, or describe a
      pre-transcode original).
      Integrity is **progressive** — a metadata probe or sampled decode can
      never claim full-file validity — and splits into two fields so an
      intentional cap is never conflated with a failure:
      `integrity.status` = `probe_ok` / `sampled_decode_ok` /
      `full_decode_ok` / `decode_failed` / `unsupported`, and
      `integrity.coverage` = `sampled` / `full` / `capped` / `partial`
      (plus error detail on failure — `partial` added in round 6: a
      decoder that dies mid-file fits none of the other three). A 45-min
      file whose first 30 min decoded cleanly is
      `sampled_decode_ok`+`capped`; a 5-min file dying at 2 min is
      `decode_failed`+`partial` — the current code treats both as the same
      silent `break` (`dsp.py:87`, `dsp.py:116-117`). Container-vs-suffix mismatch
      is *not* an integrity state (a valid AAC stream under a `.flac` name
      is structurally fine) — it lives on the delivery flag below.
      A codec that contradicts the media-server metadata sets
      `delivery.codec_mismatch` — named for the *observation*, per round 5,
      not for one interpretation (server-side transcoding is only one
      cause, alongside wrong metadata, renamed files, container swaps);
      `delivery.possible_server_transcode` is set additionally only when
      other evidence supports it. Either way it never implies the library
      file is fake — the README already warns about Navidrome transcoding. Recognize `dsf`/`dff`
      **before decoding** and return an explicit unsupported-format status —
      today the audioread/ffmpeg fallback silently converts DSD to high-rate
      PCM and the cutoff logic reads modulator noise as full-bandwidth
      content, so DSD (even lossy-sourced fake DSD) passes as CLEAN. An
      honest "not analyzed" beats a confidently wrong verdict until the
      Phase-2 DSD branch exists. *Cost: negligible (metadata only — no extra
      decode pass in the default scan).*

- [ ] **Robust cutoff detection.**
      Require a minimum contiguous bandwidth **specified in hertz** and
      converted to a bin count from the file's actual sample rate (bin
      width is `sr/4096`, so a fixed bin count would mean ~160 Hz at
      44.1 kHz but ~1.2 kHz at 96 kHz) instead of 3 bins — one initial
      value picked during fixture calibration (start at ~250 Hz), not a
      3× design range left open; apply light spectral smoothing before
      edge search. Pilot tones get the minimum treatment: excluded from
      the cutoff plus one nullable flag
      (`narrow_high_frequency_tone_present`) — no dedicated
      tone-reporting feature. The wide-band rule must **not** demand every
      bin above threshold — real spectra have harmonic gaps and notches, so
      close small gaps in the mask and require a high occupied fraction
      (~75 %) of the candidate band instead of an unbroken run. Round-6
      consistency rule: **every frequency-domain width — smoothing window,
      maximum closed gap, edge-measurement bands — is specified in hertz**
      and converted to bins per file, exactly like the cutoff width; a
      fixed 5-bin smoother would silently vary from ~54 Hz at 44.1 kHz to
      ~234 Hz at 192 kHz. Occupied fraction stays dimensionless. Cutoff
      width, smoothing and edge-sharpness thresholds interact — recalibrate
      them **together**: smoothing the profile while keeping the old
      sharpness thresholds silently changes what "sharp" means. Keep the
      existing reference/threshold model otherwise. *Cost: negligible.
      Recalibrate against `tests/fixtures/` and update `tests/README.md`
      constants.*

- [ ] **Codec-aware transcode gating.** *(Moved here from "rejected" — the
      original rejection was factually wrong.)* `_expected_cutoff_for_bitrate`
      is not cosmetic: its output directly gates the `TRANSCODED_LOSSY`
      verdict (`dsp.py:365-370`) and the one MP3-shaped table is applied to
      every lossy suffix — AAC, Opus, Vorbis, WMA, Musepack, and `m4a`/`mp4`/
      `ogg` containers whose actual codec is unknown. Encoders like ffmpeg's
      native AAC or WMA legitimately cut lower than MP3 at the same declared
      bitrate → false TRANSCODED_LOSSY. Fix: after the codec probe, apply the
      verdict-changing gate **only to codecs the table is calibrated for**
      (MP3 today); for uncalibrated codecs emit an evidence note ("bandwidth
      lower than expected for declared bitrate; no calibrated mapping for
      this codec") without changing the verdict. Codec identification for
      m4a/mp4/ogg comes from the ffprobe fallback in the probe item above;
      degrade to "unknown codec, gate disabled" when unavailable. Even for
      MP3, treat the gate as strong evidence rather than proof — the table
      is calibrated on LAME fixtures from one source, and an intentionally
      low-passed first-generation encode is a legitimate alternative
      explanation: reduce the verdict's confidence and say so in the note.
      **Round-5 pushback recorded:** the reviewer proposed demoting
      TRANSCODED_LOSSY to a CONSISTENT_LOSSY-plus-warning until
      multi-encoder fixtures exist. Rejected — that regresses a *shipped,
      fixture-validated* detector (the 128-as-320 fixture passes today),
      and TRANSCODED_LOSSY is in the suspect counts while
      CONSISTENT_LOSSY is not, so the demotion would silently hide real
      transcodes from the UI. Instead: keep the verdict at reduced
      confidence, and let the new intentionally-low-passed-MP3 fixture
      arbitrate — with round 6's discipline: if it triggers a false
      TRANSCODED_LOSSY, *diagnose before retuning* (encoder? bitrate mode?
      the intentional low-pass itself?), verify any margin change still
      separates the true-transcode fixture, and if the two can't be
      separated reliably, reduce confidence or disable the gate for that
      ambiguous bitrate range — never tune until both fixtures merely
      happen to pass. The verdict itself is not removed. *Cost:
      negligible; it removes false positives rather than adding
      analysis.*

- [ ] **Distributed segment sampling.**
      Replace the single fixed-offset window with several shorter windows
      spread through the track (e.g. 4–5 × 10–15 s at ~10/30/50/70/85 %).
      Each window is analyzed **independently** — never concatenate distant
      audio before the STFT (boundary transients). Skip only *near-silent*
      windows (quiet classical/ambient passages are diagnostically valid).
      Report per-window cutoffs, consensus, and the minimum — a consensus
      alone must not hide one low-bandwidth window; disagreement sets
      `windows_agree=false` — surfaced in the UI as a reason to deep-scan
      manually (and an escalation trigger if/when auto-escalation lands;
      this *is* the splice detector for now — see the deferred splice
      item). **Decoder strategy (revised in round 4 for uniform
      coverage):** `librosa.load(offset=...)` on lossy formats decodes
      from the file start to the offset, so scattered windows via librosa
      would cost more than one segment for mp3/m4a — use SoundFile seeking
      for natively readable formats (5 windows) and **ffmpeg input
      seeking** (`-ss X -t N` to raw PCM) for the rest, starting at
      **3 windows (~20/50/80 %)** per round 5: each window is a subprocess
      spawn, and "neutral cost" is not yet demonstrated for 5 spawns ×
      10 k compressed tracks — the m4a perf spot check decides whether to
      raise it to 5. Still far better coverage than today's single fixed
      window; fall back to fewer/earlier windows only when ffmpeg is
      absent; no multi-window ffmpeg filter graphs (process-spawn savings
      aren't worth the parsing/error-handling complexity). Output stays
      small: window timestamps, per-window cutoff, consensus, minimum,
      agreement flag — no clustering. Also fixes the "middle of
      track" docstring lie. *Cost: neutral or lower (~50–60 s decoded vs
      90 s).*

- [ ] **Structured evidence flags — nullable, additive.**
      The details JSON already exists — **add** namespaced keys
      (`analysis_rev`, `integrity`, `evidence`, `windows`) next to the
      existing ones rather than restructuring the whole payload; old rows
      keep their old shape until the rev bump reanalyzes them, and the UI
      never needs a dual-format migration. **Every fact has exactly one
      owner** (round-5 rule — duplicated facts drift apart after partial
      updates): decode state lives in `integrity.*`, codec mismatch in
      `delivery.*`, window agreement in `windows.agree`, padding scope in
      `bit_depth.*`. The `evidence` namespace holds only flags with no
      dedicated section (`edge_machine_like`, `shelf_digitally_silent`,
      `alias_image_detected`, `narrow_high_frequency_tone_present`, ...)
      alongside the verdict. Flags are **nullable**:
      `null` = not evaluated (unsupported codec, insufficient high-band
      energy, shallow scan), `false` = tested and absent — JSON gives the
      tri-state for free, no per-flag status-enum objects needed. Makes
      every verdict auditable from the UI with no schema migration (details
      is already a JSON column). *Cost: negligible.*

- [ ] **Bit-depth histogram alongside — not instead of — the minimum.**
      Compute the trailing-zero *distribution*, but keep the two kinds of
      evidence strictly separate: the exact test (every tested nonzero
      sample padded) is the **only** trigger for the UPSCALED verdict —
      round 6 caught that this sentence still said "at 0.95", contradicting
      the round-4 numbers below; the single unambiguous rule is:
      `exact_in_sampled_window` → UPSCALED at **0.9**, `deep_eligible=true`
      (the probe reads ~30 s from the middle, `dsp.py:212`);
      `exact_full_file` (full-decode confirmation) → UPSCALED at **0.95**;
      statistical evidence alone → never UPSCALED. (Worth recording why the
      risk is smaller than the reviewer's framing: a file whose sampled
      window is padded but whose other sections carry genuine deeper bits
      is still substantively an upscale in the sampled region — the exotic
      failure mode is a compilation splicing padded and true-24-bit
      sources, which the UPSCALED-at-0.9 wording tolerates fine.)
      A statistical result (e.g. 99.9 % of
      samples ≤ 16 bits but a few genuinely deeper — dither, gain changes,
      SRC, edits) is reported as `predominant_bit_depth` +
      `lower_bit_activity_fraction` evidence with a note, never as
      deterministic padding. Column meanings (`container_bits`/
      `effective_bits`) stay unchanged. *Cost: negligible — same samples,
      one histogram.*

- [ ] **Cheap render/IO wins.**
      Drop PNG `compress_level` 9 → 6 (large CPU saving, small size delta);
      add a settings toggle "skip spectrogram for CLEAN tracks" (default off;
      full removal is a UX regression — audio isn't retained after analysis,
      so a spectrogram not rendered at scan time can never be rendered).
      *Cost: negative (saves CPU).*

- [ ] **Minimum adversarial fixtures — same PR as the features they test.**
      Phase 1 changes verdict behavior, so the fixture set must grow with
      it — scoped to what Phase 1 actually builds, not the full Phase-3
      corpus: a narrow ultrasonic pilot tone (robust-cutoff test), AAC and
      ALAC in m4a + Opus/Vorbis in ogg (codec probe/gating), an
      intentionally low-passed first-generation MP3 (gating
      false-positive), truncated FLAC and MP3 (integrity states), and a
      dithered 16→24 conversion (bit-depth histogram). Round-5 correction
      on the truncation fixtures: their expectations are **per mode** — a
      tail-truncated file that probes fine and whose sampled windows all
      decode is *correctly* `sampled_decode_ok`+`sampled` in Phase 1 (the
      test asserts Phase 1 never claims `full_decode_ok`, not that it
      catches every truncation); only the full-decode/deep expectation is
      `decode_failed`. That's the progressive-integrity contract doing its
      job, not a detection miss. Rejected from
      review: a formal calibration/holdout corpus split — for a
      hand-tuned-threshold plugin maintained by one person, the existing
      fixtures-plus-documented-constants discipline is the right weight.
      One lightweight rule adopted in its place: **an existing fixture's
      expected verdict is never changed just to make a new threshold pass**
      — only when the old expectation is shown to be wrong.
      *Cost: development-time only.*

## Phase 2 — deeper analysis in deep mode (after Phase 1 stabilizes)

Deep mode stays **manually triggered** for this phase (per-track button,
bulk button, `windows_agree=false` and "dark master?" results surfaced in
the UI as the candidates worth pressing it on). Auto-escalation moved to
DEFERRED — see below.

- [ ] **Global streaming percentile — the corrected deep profile.**
      *(Round-4 simplification adopted — it replaces the round-3
      "occupancy + sustained bandwidth" design with something strictly
      simpler.)* The real defect is that the deep profile is a
      max-of-chunk-95th-percentiles (`dsp.py:96`), which no percentile
      semantics survive. Fix it directly: per frequency bin, accumulate a
      quantized dB-bucket histogram over **all frames** (round-5 precision
      fix: not "active" frames — an activity gate would smuggle in exactly
      the new tunable this design exists to avoid, and the current
      per-chunk percentile is computed over all frames, so all-frames
      preserves the existing semantics on a whole-scan basis), and recover
      an approximate **whole-scan 95th percentile** at the end. Histogram
      quantization is fixed and documented — one generous dB range and
      ~0.5 dB buckets, no configurable bucket counts, adaptive ranges or
      percentile modes — and validated once against `np.percentile` on the
      committed fixtures (difference must be negligible). That profile —
      not the max-hold — feeds `_find_cutoff`/`_verdict`, which then run
      **unchanged**. The max/peak profile is kept only as an evidence
      field (peak vs percentile gap = anomaly hint). *Cost: ~5–15 % of
      deep scan only; memory bounded by bins × buckets of ints.*

- [ ] **Memory-bounded deep chunks.**
      The fixed 120 s chunk (`dsp.py:33`) makes peak memory scale with
      sample rate: at 192 kHz one chunk is ~23 M samples and the
      STFT + magnitude + dB matrices reach hundreds of MB per worker. Size
      chunks by a *sample budget* instead:
      `chunk_seconds = min(120, target_samples / sr)` — same analyzed
      duration, flat memory ceiling. Round-4 caveats accepted: it's not
      literally one line — keep a minimum chunk duration, count channels
      in the sample budget once per-channel analysis exists, make sure the
      display-matrix accumulation stays bounded too, and drop each chunk's
      matrices before loading the next. The separate high-rate/DSD
      concurrency pools proposed alongside it are still rejected (worker
      count and `MAX_QUEUED_ANALYSIS_JOBS` already bound concurrency).
      *Cost: none — pure peak-memory fix.*

- [ ] **Two-stage per-channel analysis in deep mode.**
      Keep mono for the default scan. In deep mode, *always* compute the
      cheap per-channel statistics (RMS, DC, sample peak, correlation,
      polarity — near-free during the decode), and run separate
      **per-channel spectra** only on a *tight* trigger (round-5
      correction — plain "low stereo correlation" fires on legitimate wide
      stereo, reverb and hard panning, which would double FFT cost
      routinely): a suspicious mono cutoff, a nearly absent channel, a
      large RMS imbalance, or *strongly negative* correlation (polarity
      problem) — not merely low. This avoids doubling spectral CPU on
      every deep scan while keeping the trigger cheap.
      Combination rule unchanged from round 3: channel *consensus* when
      they agree, a channel-anomaly report when they don't — never a
      silent max (one channel's interference or hiss must not launder the
      track). *Cost: <5 % typically; ~2× spectral CPU only on the subset
      that triggers stage two.*

- [ ] **Basic waveform-quality metrics during deep decode.**
      While the deep scan already streams the whole file: sample peak, RMS,
      crest factor, DC offset, clipping runs (consecutive full-scale), exact
      zero runs / dropouts, leading/trailing silence. Record coverage with
      the metrics: frames analyzed vs declared, and whether the 30-min cap
      truncated them — a peak measured over half the file must say so.
      Stored under a separate `quality` key in details — never mixed into
      the authenticity verdict. This is the useful core of suggestions 13/14
      without a scope explosion. *Cost: <5 % on top of the decode we're
      already doing.*

- [ ] **DSD sampled validation — optional milestone, DSF only.**
      *(Round 4 correctly notes this doesn't answer the plugin's core
      authenticity question — it's an honest-inconclusive upgrade, so it's
      last in Phase 2 and skippable.)* The mandatory part already lives in
      Phase 1: the DSF/DFF guard, ffprobe-read metadata — limited to what
      ffprobe actually provides (DSD rate, channels, duration, physical
      file size; round 6 correctly notes ffprobe does *not* expose the DSF
      header's declared-size field or chunk boundaries — attributing that
      to Phase 1 would quietly require a DSF parser split across two
      phases) — INCONCLUSIVE verdict, and a substatus showing source
      validation was not performed. This optional milestone owns *all*
      DSF-header work: the minimal header parse, declared-vs-actual size,
      chunk validation, plus a few distributed windows
      decoded through ffmpeg, and audible-band checks (clipping, silence,
      channel balance) — **after an explicit audible-band low-pass and
      resample**, since ffmpeg's DSD output carries heavy ultrasonic
      modulator noise that would otherwise inflate peak/RMS and fake
      clipping. Results are substatuses under `dsd.*`; primary verdict
      stays INCONCLUSIVE. Never run PCM cutoff heuristics on DSD output,
      and **exclude DSD from the ultrasonic-evidence item below**. DFF
      structural validation is deferred. *Cost: moderate, DSD files only;
      zero effect on FLAC/MP3 scans.*

- [ ] **Ultrasonic band evidence (PCM > 48 kHz) — optional experiment.**
      On the deep scan's existing per-second spectra, track coarse bands
      (20–24, 24–30, 30–40, 40–48 kHz, …): median level, temporal
      variance, active-frame share, correlation with audible-band level.
      Round-4 epistemics adopted: report **observed behavior classes**
      (`high_band_behavior`: static / level-correlated /
      transient-correlated / narrow-tonal / insufficient-evidence) rather
      than a `signal_related_bandwidth` claim — level-correlated
      ultrasonic energy is not proof of musical content (saturation,
      dynamics processing and converter noise all track level), so
      "dynamic energy present in the 30–40 kHz band" is the honest output,
      never a precise Hz figure. Evidence fields plus a note; no verdict
      change; PCM only (DSD excluded, see above); not required for the
      deep-scan rewrite to ship. The ambiguous high-rate "dark master?"
      CLEANs reach it via the manual deep button (they're surfaced in the
      UI as candidates). *Cost: ~10–30 % on high-rate deep scans only;
      reuses existing STFT frames.*

- [ ] **Alias-image energy-floor guard (everything else deferred).**
      Alias correlation is already verdict-affecting today (corr ≥ 0.6
      drives UPSAMPLED at 0.9 confidence), so one guard ships now: require
      both mirrored bands to sit meaningfully above the numerical floor,
      and return the correlation as *not evaluated* (null) when either
      band lacks energy — a few lines protecting a live false-positive
      path. Round-5 distinction accepted: **detrending is not hygiene** —
      it changes the correlation feature itself and its distribution,
      which means recalibrating the 0.6 threshold and re-reviewing
      fixtures, so it moves to the deferred redesign along with the
      multi-window work. Reassess after distributed sampling, the global
      percentile and codec gating land. *Cost: near-zero.*

## Phase 3 — long-term / opt-in

- [ ] **Loudness (integrated LUFS, LRA) and oversampled true peak** in deep
      mode only, reported under `quality`. Needs a K-weighting implementation;
      moderate effort, clear value for the mastering-QC use case.
- [ ] **Evidence-tier confidence labels.** Map the numeric confidence to
      tiers (deterministic / strong / moderate / inconclusive) in the UI and
      stop implying calibrated probability. Full calibration requires the
      corpus below.
- [ ] **Test-corpus expansion** (multiple genres/decades, LAME + Fraunhofer,
      Apple/FFmpeg AAC, Opus/Vorbis, several resamplers, vinyl/tape
      transfers). Prerequisite for any calibrated confidence and for the
      lossy-artifact detector idea. High effort, no runtime cost; grow it
      incrementally as false positives/negatives are found in the wild.
- [ ] **Client-rendered spectrograms** (store a quantized matrix, render in
      the browser) — only if DB size becomes a real complaint; it's a UI
      project, not a DSP one.

## DEFERRED — will implement only if its unlock condition is met

Not scheduled in any phase. Each item names the concrete condition that
would bring it back:

- [ ] **Heavy DSD forensics** (raw one-bit entropy/modulator-health
      analysis, noise-shaping characterization, native-DSD-vs-PCM hard
      classification, full-rate DSD spectrograms). 2–5× analysis cost,
      specialized audience. *Unlock: demonstrated demand from real DSD
      libraries after basic Phase-2 DSD support ships.*
- [ ] **Hard ultrasonic source classification** (automatic native-vs-PCM
      verdict from the Phase-2 band measurements). *Unlock: the Phase-3
      calibration corpus exists — its error rate is unknowable without it.*
- [ ] **Lossy-artifact detector for high-bitrate transcodes (suggestion
      9).** The one genuinely hard open problem (AAC-256→FLAC passes
      today, by documented design); every proposed feature (block-period
      modulation, pre-echo, spectral holes) is false-positive-prone.
      *Unlock: Phase-3 corpus first, then only as an experimental
      evidence-only deep-mode score, never a direct CLEAN→FAKE_SUSPECT
      flip.*
- [ ] **Expanded resample candidate rates (part of 11).** Current
      `_STD_NYQUISTS` covers source rates up to 96 kHz; only 352.8/384 kHz
      containers are missed — vanishingly rare. *Unlock: none needed —
      fold it in opportunistically next time `_STD_NYQUISTS` is touched
      (derive candidates from a standard-rate list instead of hardcoding
      four values).*
- [ ] **Auto-escalation (Phase-1 results → automatic deep scans).**
      Moved out of Phase 2 in round 4: trigger design, dedup, caps and
      inconclusive handling are real operational logic, and the honest
      sequencing is to watch which results users *manually* deep-scan
      first. The guard design (default-off setting, explicit trigger list
      incl. window disagreement and "dark master?" CLEANs, per-scan cap,
      `analysis_rev`-keyed idempotency, enqueue after the parent scan) is
      kept here as the spec. *Unlock: the rewritten deep analyzer is
      stable and manual deep-scan patterns confirm the trigger list.*
- [ ] **Splice/mixed-source transition detection.** Timestamps are cheap;
      *reliable* transitions (plateau detection, hysteresis, minimum
      duration, arrangement-change suppression) are not. Phase 1's
      `windows_agree=false` is the splice detector for now. When the edge
      series is next touched anyway, retain `(second, cutoff, level)`
      tuples — one line that keeps this implementable later (today
      quiet-second drops make index ≠ time, `dsp.py:103-109`). *Unlock: a
      real mixed-source file where window disagreement proves
      insufficient.*
- [ ] **I/O consolidation / full single-streaming-decoder / unified ffmpeg
      backend.** (Absorbed the former Phase-2 I/O bullet in round 4: the
      MD5 re-read is warm-cache and cannot ride the decode pass anyway —
      `file_md5` hashes encoded bytes, `jobs.py:392` — and folding the
      bit-depth probe into a shared reader starts building the very
      abstraction this item defers. If it falls out naturally of the
      SoundFile-based distributed sampling, fine; don't pursue it.)
      *Unlock: DSD, per-channel and distributed sampling have all landed
      and maintaining separate decode paths has become the more complex
      option.*
- [ ] **Alias-image redesign: detrending + multi-window correlation.**
      Only the energy-floor guard stays in Phase 2; detrending joined the
      redesign in round 5 because it changes the correlation feature's
      distribution and forces threshold recalibration. *Unlock: alias
      correlation still causes verdict errors after distributed sampling,
      the global percentile and codec gating have landed.*
- [ ] **DFF structural validation.** DSF covers the dominant DSD case.
      *Unlock: demonstrated DFF demand after the optional DSF milestone
      ships.*

## REJECTED — will NOT be implemented

- **Four-axis verdict *schema*** (separate DB verdicts for integrity /
  authenticity / quality / provenance). Right diagnosis, too-heavy
  prescription: it breaks the existing UI, filters, `bad_expr` counts and
  the DB in one go. The *substance* is adopted instead — integrity status
  (Phase 1), quality metrics (Phase 2), evidence flags (Phase 1) — kept as
  distinct namespaces inside the details JSON (`integrity.*`, `quality.*`,
  evidence flags) under one primary verdict the UI already understands, so
  a corrupt file reads as corrupt — not fake-lossless — and a valid but
  crushed master reads as both CLEAN and loudness-warned.
- **Per-codec bitrate wording tables** (the cosmetic half of suggestion
  10). The verdict-affecting half moved to Phase 1; codec-specific
  wording tables add maintenance without changing any outcome.
- **Per-track true peak, full-track high-rate FFTs, neural classifiers,
  multiple full decode passes in the default scan.** The suggestion itself
  lists these as anti-features; agreed. (True peak still arrives in
  Phase 3 — but deep mode only, never the library scan.)

## Cross-cutting rules

- Any change to `dsp.py` thresholds or detection logic must be re-run against
  `tests/fixtures/` (`python3 -m unittest discover tests -v`) and the measured
  calibration constants in `tests/README.md` updated in the same PR.
- Lightweight performance guardrails (in place of the rejected formal
  benchmark matrix): the default scan stays within ~10–15 % of current wall
  time on a 16/44.1 FLAC, and peak memory must not scale with sample rate
  (the chunk sample budget enforces this). Round 4 added two targeted spot
  checks the FLAC test can't catch: one m4a (exercises the ffprobe +
  ffmpeg-seek path) and one 24/192 FLAC deep scan (exercises the memory
  budget). Spot-check when a phase lands; no CI gate.

## Round-3 feedback disposition

Third review round, summarized. Adopted (already folded into the items
above): analysis-revision stamp; substatus/INCONCLUSIVE verdict contract
with an explicit deep-scan trigger set; progressive integrity states with
cap-vs-error distinction; ffprobe as probe fallback instead of a custom MP4
parser (ffmpeg verified in the AudioMuse Dockerfile); stream-bitrate
reliability for the transcode gate; delivery-transcode flag; gap-tolerant
cutoff mask with joint recalibration; independent (never concatenated)
sampling windows with min/consensus reporting; nullable evidence flags;
sampled-vs-full-file zero-padding scope; sustained-occupancy-driven deep
verdict; sample-budgeted deep chunks; timestamped edge series; per-channel
consensus instead of max; DSD audible-band filtering and exclusion from
generic ultrasonic logic; band-class (not Hz-precise) ultrasonic reporting;
alias energy-floor checks; escalation guards; scoped adversarial fixtures;
quality-metric coverage fields.

Rejected as complexity this plugin doesn't need:

- **A separate Phase 0.** Its two real items (revision stamp, verdict
  contract) are simply the first two Phase-1 entries; the rest is ceremony.
- **Formal benchmark acceptance matrix** (7 file classes × 8 metrics with
  gates). Replaced by the two guardrails above.
- **Calibration/holdout/regression corpus splits.** Right for a trained
  model; wrong weight for hand-tuned heuristics with documented constants.
- **Per-flag status-enum evidence objects.** JSON `null` already encodes
  "not evaluated"; nested `{status, ...}` objects per flag is schema bloat.
- **Dedicated high-rate/DSD concurrency pools.** Worker count and
  `MAX_QUEUED_ANALYSIS_JOBS` already bound concurrency; the memory fix is
  the chunk budget, not a new scheduler.
- **Phase-1 ultrasonic evidence for all hi-res files.** The escape path is
  real, but routing "dark master?" CLEANs through the escalation trigger
  closes it at near-zero default-scan cost.
- **Downgrading the sampled zero-padding verdict.** See the bit-depth item:
  scope is now labelled honestly, but the 0.95 verdict stands.
- **Regression-residual ultrasonic modeling and a silence-type taxonomy for
  quality metrics.** Refinements without a demonstrated failure case;
  revisit if the simple versions misfire in practice.
- **Distributing the deep-scan budget across >30-min tracks.** Nice for DJ
  mixes, but it conflicts with sequential-decode consolidation on lossy
  formats (seeking = decode-from-start); the cap-reached flag plus manual
  deep scan covers the rare case for now.

## Round-4 feedback disposition

The fourth round was a reduction pass — mostly correct, and its direction
(shrink Phase 2) matches this plan's own scope rule. Adopted:

- **Revision check on both skip paths** — a genuine catch; the round-3
  wording covered only the verify/MD5 path, and `changed` mode skips on
  fingerprint alone before any download (`jobs.py:504`).
- **`deep_eligible` routing flag** — fixed a real contradiction between
  "INCONCLUSIVE is deep-ineligible" and "the deep-mode DSD branch analyzes
  INCONCLUSIVE DSD files".
- **Integrity = status + coverage** — an intentional cap is not a partial
  failure; container mismatch is a delivery flag, not an integrity state.
- **Global streaming percentile** instead of occupancy + sustained-bandwidth
  threshold — the rare review suggestion that *removes* a tunable while
  fixing the defect; `_find_cutoff`/`_verdict` run unchanged on a corrected
  profile.
- **ffprobe fast path** (no subprocess per FLAC/MP3), **ffmpeg input
  seeking** for lossy sampling windows (uniform coverage instead of a
  codec-dependent bias), **two-stage per-channel** analysis, **additive**
  details JSON, single initial cutoff-width value, the
  fixture-no-retune rule, two extra perf spot checks, behavior-class
  ultrasonic wording (`high_band_behavior`, no `signal_related_bandwidth`
  claim), and the 0.9-sampled / 0.95-full-file zero-padding split.
- **Deferrals**: auto-escalation (watch manual deep-scan patterns first),
  splice transition detection (`windows_agree` is the splice detector for
  now), I/O consolidation (absorbed into the unified-backend item), the
  alias multi-window redesign, DFF validation. DSD sampled decode and
  ultrasonic band evidence stay in Phase 2 but marked optional.

Pushed back on:

- **Moving all alias work to Phase 3.** The energy-floor guard protects a
  path that changes verdicts *today* (corr ≥ 0.6 → UPSAMPLED at 0.9) and
  costs a few lines; only the redesign is deferred.
- **The bit-depth risk framing.** The compromise numbers are adopted
  because they cost nothing, but the failure cases the review lists
  (editing, fades, partial dither) mostly still leave UPSCALED
  substantively correct for the sampled region; the genuinely wrong case
  (a compilation splicing padded and true-24-bit sources) is exotic. The
  0.9→0.95 split is bookkeeping honesty, not a reliability concession.
- **"Remove crest factor."** It's one division on numbers already
  computed; removing it is scope theater, not scope control.

## Round-5 feedback disposition

Fifth round — convergence. All six issues were precision-level; five
adopted, one pushed back on. The review also endorsed all four round-4
pushbacks, so the open-disagreement list is empty except for the item
below. **This plan is now considered implementation-ready; further review
rounds are expected to yield diminishing returns.**

Adopted:

- **DSD deep-eligibility keys on implemented capability**, not ffmpeg
  presence — a real logic gap: with the DSD milestone optional, "ffmpeg →
  eligible" would offer a deep scan that can't add anything.
- **Global percentile pinned down**: all frames (an "active frame" gate
  would smuggle back the tunable the design exists to avoid, and current
  chunk percentiles use all frames anyway), fixed documented histogram
  range/bucket width, one-time validation against `np.percentile` on
  fixtures.
- **Three ffmpeg windows for compressed formats initially** — 5 subprocess
  spawns × 10 k tracks is unproven cost; the m4a spot check arbitrates a
  raise to 5. No multi-window filter graphs.
- **Alias hygiene = energy floor only** — the reviewer's distinction is
  correct: the floor guard validates the existing feature, detrending
  *changes* it (recalibration required), so detrending joins the deferred
  redesign.
- **Per-mode truncation-fixture expectations** (Phase 1 sampled decode
  legitimately passes a tail-truncated file; the test asserts it never
  claims `full_decode_ok`), **single-owner JSON facts** (no
  `decode_complete`/`suffix_matches_codec`/`windows_agree` duplicates in
  `evidence`), **`delivery.codec_mismatch` rename** (observation, not
  interpretation), **tightened per-channel stage-2 trigger** (strongly
  negative correlation, absent channel, RMS imbalance, suspicious mono
  cutoff — not plain "low correlation", which fires on legitimate wide
  stereo).

Pushed back on:

- **Demoting TRANSCODED_LOSSY to CONSISTENT_LOSSY + warning.** That
  regresses a shipped detector validated by the 128-as-320 fixture, and —
  decisively — TRANSCODED_LOSSY is in the suspect counts while
  CONSISTENT_LOSSY is not, so the demotion would silently drop real
  transcodes out of the UI's suspect view. Adopted instead: reduced
  confidence + alternative-explanation note, with the new
  intentionally-low-passed-MP3 fixture as the arbiter — a false positive
  there retunes the 1.5 kHz margin, not the verdict's existence.

## Round-6 feedback disposition (terminal)

All six corrections adopted — each removes an ambiguity rather than adding
capability, and two fixed real contradictions this document had
accumulated:

- **Lazy ffprobe for the MP3 gate.** Genuine catch: "plain MP3 skips
  ffprobe" and "the gate prefers a probed bitrate" could not both hold.
  Resolution: preliminary media-server bitrate → ffprobe only when the
  preliminary verdict would be TRANSCODED_LOSSY → confirm; no reliable
  bitrate, no gate. Suspicious-only subprocesses, verdict preserved.
- **`coverage = partial`** — a decoder dying mid-file fit none of
  sampled/full/capped.
- **All spectral widths in hertz** (smoothing, gap closing, edge bands) —
  the same sample-rate-independence principle already accepted for cutoff
  width, applied consistently.
- **DSF metadata honesty** — ffprobe gives rate/channels/duration, not the
  DSF header's declared size; all header parsing now lives in one place
  (the optional milestone), not split across phases.
- **The stale "0.95 sampled" sentence** — editorial leftover from round 3,
  contradicting the round-4 0.9/0.95 split; replaced with one rule.
- **INCONCLUSIVE visibility** — excluded from suspect counts must not mean
  invisible; one count + one filter so a "clean scan" can't silently omit
  never-analyzed files.
- Also adopted: the diagnose-before-retuning discipline for the
  low-passed-MP3 fixture (a single fixture must not dictate a global
  margin; separability from the true-transcode fixture is the bar).

Review closed at the reviewer's own recommendation: six rounds converged
from architecture (rounds 1–3) through scope (4) and precision (5–6) to
zero open disagreements. Next validation happens against the Phase-1 code
and tests, starting with the analysis-revision stamp.
