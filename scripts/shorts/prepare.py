"""Cut a clip's source segments into one intermediate file, trimming long silences.

- segments: list of [start, end] in source seconds (lets a clip skip a tangent)
- long silences (silencedetect) are shortened, but never where a word is spoken
- returns a time map [(src_start, src_end, out_start), ...] so word timings,
  zoom moments and SFX can be moved into output time
"""
import json
import re
import subprocess
from pathlib import Path


def ffmpeg_bin():
    return 'ffmpeg'


def detect_silences(src, start, end, noise_db=-32, min_dur=0.55):
    cmd = [ffmpeg_bin(), '-nostdin', '-hide_banner', '-ss', f'{start:.3f}', '-t', f'{end - start:.3f}',
           '-i', str(src), '-vn', '-af', f'silencedetect=n={noise_db}dB:d={min_dur}', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r'silence_start: (-?[\d.]+)', line)
        if m:
            cur = start + max(0.0, float(m.group(1)))
        m = re.search(r'silence_end: ([\d.]+)', line)
        if m and cur is not None:
            sil.append((cur, start + float(m.group(1))))
            cur = None
    return sil


def plan_keep_ranges(segments, words, src, trim_silence=True, keep_pad=0.14, min_dur=0.55):
    """Split each segment into keep-ranges with long silences shortened."""
    keep = []
    for s, e in segments:
        if not trim_silence:
            keep.append((s, e))
            continue
        cuts = []
        for a, b in detect_silences(src, s, e, min_dur=min_dur):
            a2, b2 = a + keep_pad, b - keep_pad
            if b2 - a2 < 0.2:
                continue
            # never cut through a spoken word (guards against quiet speech/laughs)
            if any(a2 < w['s'] < b2 or a2 < w['e'] - 0.05 < b2 for w in words):
                continue
            cuts.append((a2, b2))
        t = s
        for a, b in cuts:
            if a > t + 0.05:
                keep.append((t, a))
            t = b
        if e > t + 0.05:
            keep.append((t, e))
    return keep


def build_intermediate(src, keep_ranges, out_path, segments):
    """Render keep-ranges into one file. One input per original segment (fast seeking)."""
    inputs, filters, labels = [], [], []
    seg_of = []
    for k, (s, e) in enumerate(segments):
        inputs += ['-ss', f'{s:.3f}', '-t', f'{e - s:.3f}', '-i', str(src)]
    n = 0
    for (a, b) in keep_ranges:
        k = next(i for i, (s, e) in enumerate(segments) if s - 0.01 <= a <= e + 0.01)
        s0 = segments[k][0]
        ra, rb = a - s0, b - s0
        d = rb - ra
        fade = min(0.012, d / 4)
        filters.append(f'[{k}:v]trim=start={ra:.3f}:end={rb:.3f},setpts=PTS-STARTPTS[v{n}]')
        filters.append(f'[{k}:a]atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS,'
                       f'afade=t=in:d={fade:.3f},afade=t=out:st={d - fade:.3f}:d={fade:.3f}[a{n}]')
        labels.append(f'[v{n}][a{n}]')
        n += 1
    filters.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[v][a]")
    cmd = [ffmpeg_bin(), '-nostdin', '-hide_banner', '-v', 'error', '-y', *inputs,
           '-filter_complex', ';'.join(filters), '-map', '[v]', '-map', '[a]',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-pix_fmt', 'yuv420p',
           '-c:a', 'pcm_s16le', '-ar', '48000', str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f'intermediate render failed: {r.stderr[-1500:]}')
    timemap, t = [], 0.0
    for a, b in keep_ranges:
        timemap.append((a, b, t))
        t += b - a
    return timemap


def prepare(src, segments, words, work_dir, trim_silence=True):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / 'intermediate.mkv'
    key = json.dumps({'src': str(src), 'segments': segments, 'trim': trim_silence})
    cache = work_dir / 'timemap.json'
    cached = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else None
    if isinstance(cached, dict) and cached.get('key') == key and out.exists():
        timemap = cached['timemap']  # same cut as last run: reuse the intermediate
    else:
        keep = plan_keep_ranges(segments, words, src, trim_silence=trim_silence)
        timemap = build_intermediate(src, keep, out, segments)
        cache.write_text(json.dumps({'key': key, 'timemap': timemap}), encoding='utf-8')
    removed = sum(e - s for s, e in segments) - sum(b - a for a, b, _ in timemap)
    return {'path': str(out), 'timemap': timemap, 'removed_silence_sec': round(removed, 2),
            'cut_points': [o for _, _, o in timemap[1:]]}
