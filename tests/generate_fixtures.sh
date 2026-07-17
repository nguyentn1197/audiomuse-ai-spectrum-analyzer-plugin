#!/usr/bin/env bash
# Generate ground-truth test fixtures from one confirmed-genuine hi-res file.
# Usage: ./generate_fixtures.sh <source-audio> <output-dir> [ffmpeg] [start-s] [duration-s]
# Requires ffmpeg with libmp3lame (a johnvansickle.com static build works).
# The committed fixtures were built from a confirmed-genuine DSD64 file with
# start=60 duration=45 (small enough to live in the repo).
set -euo pipefail
SRC=$1
OUT=$2
FF=${3:-ffmpeg}
START=${4:-0}
DUR=${5:-}
mkdir -p "$OUT"
FLAGS="-hide_banner -loglevel error -y"
CUT="-ss $START"
[ -n "$DUR" ] && CUT="$CUT -t $DUR"

# reference: genuine hi-res master (DSD sources get a 40 kHz lowpass)
$FF $FLAGS $CUT -i "$SRC" -af "lowpass=f=40000" -ar 96000 -sample_fmt s32 -c:a flac "$OUT/genuine_dsd_2496.flac"
M="$OUT/genuine_dsd_2496.flac"

# genuine CD-quality downconvert (16-bit, dithered)                -> CLEAN
$FF $FLAGS -i "$M" -ar 44100 -af "aresample=osf=s16:dither_method=triangular" -sample_fmt s16 -c:a flac "$OUT/genuine_cd_1644.flac"
# fake hi-res: 44.1k source upsampled to 96k/24                    -> UPSAMPLED
$FF $FLAGS -i "$OUT/genuine_cd_1644.flac" -ar 96000 -sample_fmt s32 -c:a flac "$OUT/fake_hires_44to96.flac"
# fake hi-res: 48k source upsampled to 96k/24                      -> UPSAMPLED
$FF $FLAGS -i "$M" -ar 48000 -sample_fmt s16 -c:a flac "$OUT/tmp48.flac"
$FF $FLAGS -i "$OUT/tmp48.flac" -ar 96000 -sample_fmt s32 -c:a flac "$OUT/fake_hires_48to96.flac"
# fake 24-bit: 16-bit samples zero-padded into 24-bit container    -> UPSCALED
$FF $FLAGS -i "$M" -sample_fmt s16 -c:a flac "$OUT/tmp16_96.flac"
$FF $FLAGS -i "$OUT/tmp16_96.flac" -sample_fmt s32 -c:a flac "$OUT/fake_24bit_96k.flac"
# fake lossless: mp3 128 re-encoded as FLAC                        -> FAKE_SUSPECT
$FF $FLAGS -i "$OUT/genuine_cd_1644.flac" -c:a libmp3lame -b:a 128k "$OUT/tmp128.mp3"
$FF $FLAGS -i "$OUT/tmp128.mp3" -sample_fmt s16 -c:a flac "$OUT/fake_lossless_128.flac"
# fake lossless: mp3 320 re-encoded as FLAC                        -> FAKE_SUSPECT
$FF $FLAGS -i "$OUT/genuine_cd_1644.flac" -c:a libmp3lame -b:a 320k "$OUT/tmp320.mp3"
$FF $FLAGS -i "$OUT/tmp320.mp3" -sample_fmt s16 -c:a flac "$OUT/fake_lossless_320.flac"
# lossy transcode: 128k source re-encoded at 320k                  -> TRANSCODED_LOSSY
$FF $FLAGS -i "$OUT/tmp128.mp3" -c:a libmp3lame -b:a 320k "$OUT/transcoded_128as320.mp3"
# honest first-generation 320k mp3                                 -> CONSISTENT_LOSSY
cp "$OUT/tmp320.mp3" "$OUT/consistent_320.mp3"
# genuine dark master: gentle analog-style rolloff on hi-res       -> CLEAN
$FF $FLAGS -i "$M" -af "lowpass=f=14000:p=1,lowpass=f=14000:p=1" -sample_fmt s32 -c:a flac "$OUT/dark_master_96k.flac"

rm -f "$OUT/tmp48.flac" "$OUT/tmp16_96.flac" "$OUT/tmp128.mp3" "$OUT/tmp320.mp3"
echo "fixtures written to $OUT"
