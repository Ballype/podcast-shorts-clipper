"""Turn a clip plan into finished vertical shorts.

    python scripts/shorts/make_short.py --plan plan.json [--only clip_01] [--fast]

plan.json (written by Claude after analysing the transcript; see SHORTS_EDITING.md):
{
  "video": {"title": "...", "url": "...", "source": "path/to/video.mp4", "subs": "path/to/video.en.vtt"},
  "output_dir": "youtube-clips/<slug>",
  "options": {...global editing options...},
  "clips": [
    {"id": "clip_01", "title": "...", "score": 92,
     "segments": [["31:22.8", "32:05"], ["32:40", "33:57.4"]],
     "emphasis": [{"t": "33:30.1", "end": "33:33", "strength": "strong"}],
     "highlight_words": ["girls", "bathroom"],
     "description": "...", "social_caption": "...", "hashtags": ["#..."],
     "options": {...per-clip overrides...}}
  ]
}
Times may be seconds or [HH:]MM:SS(.ms) strings, always in SOURCE video time.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera  # noqa: E402
import captions  # noqa: E402
import faces as faces_mod  # noqa: E402
import prepare  # noqa: E402
import qc  # noqa: E402
import render  # noqa: E402
import report  # noqa: E402
import sfx as sfx_mod  # noqa: E402
from words import parse_words, remap_time, remap_words, words_in_range  # noqa: E402

DEFAULTS = {
    'zoom': 'normal',             # off | subtle | normal | strong
    'sfx': True,
    'caption_color': None,        # e.g. "#3BA7FF"; None = auto (footage accent or yellow)
    'caption_size': 1.0,
    'caption_position': 'lower',  # lower | middle | upper
    'caption_font': 'Arial Black',
    'caption_uppercase': True,
    'words_per_caption': 3,
    'split_screen': 'auto',       # auto | off | always
    'trim_silence': True,
    'keep_original_audio': False,
    'denoise': True,
    'max_base_zoom': 1.4,
    'x264_preset': 'faster',
    'crf': 18,
}


def to_sec(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(':')]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def apply_corrections(words, corrections):
    """Fix transcript errors in caption text, e.g. {"Croninberg": "Kronenbourg"}.
    Matches whole words case-insensitively and keeps surrounding punctuation."""
    if not corrections:
        return
    fixes = {k.lower(): v for k, v in corrections.items()}
    for w in words:
        m = re.match(r'^(\W*)(.*?)(\W*)$', w['w'])
        core = m.group(2)
        if core.lower() in fixes:
            w['w'] = m.group(1) + fixes[core.lower()] + m.group(3)


def people_sheet(fdata, video_path, out_png):
    """Save a labelled crop of every person found (P0, P1, ...) so Claude can map
    people to names and set `main_speaker` / `focus` in the plan."""
    import cv2
    import numpy as np
    tp = camera.identify_persons(fdata['samples'])
    best, stats = {}, {}
    for s in fdata['samples']:
        for f in s['faces']:
            p = tp.get(f['id'])
            if p is None:
                continue
            st = stats.setdefault(p, {'x': [], 'act': [], 'shots': set()})
            st['x'].append(f['cx'])
            st['act'].append(f['act'])
            st['shots'].add(sum(1 for sh in fdata['shots'][1:] if sh[0] <= s['t']))
            if p not in best or f['w'] > best[p][1]['w']:
                best[p] = (s['t'], f)
    W, H = fdata['video']['w'], fdata['video']['h']
    tiles = []
    for p in sorted(best):
        t, f = best[p]
        # frame-accurate grab (OpenCV seeking in MKV can land on the wrong frame)
        raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{t:.3f}', '-i', str(video_path),
                              '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'],
                             capture_output=True, stdin=subprocess.DEVNULL).stdout
        if len(raw) < W * H * 3:
            continue
        fr = np.frombuffer(raw[:W * H * 3], np.uint8).reshape(H, W, 3)
        r = int(f['h'] * 1.3)
        x0, y0 = int(max(0, f['cx'] - r)), int(max(0, f['cy'] - r * 0.8))
        crop = fr[y0:min(H, y0 + 2 * r), x0:min(W, x0 + 2 * r)]
        tile = cv2.resize(crop, (260, 260))
        cv2.rectangle(tile, (0, 0), (84, 44), (0, 0, 0), -1)
        cv2.putText(tile, f'P{p}', (8, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 215, 255), 3)
        tiles.append(tile)
    if tiles:
        cv2.imwrite(str(out_png), np.hstack(tiles))
    return [{'person': p, 'shots': len(st['shots']), 'mean_x': int(np.mean(st['x'])),
             'act_median': round(float(np.median(st['act'])), 2)} for p, st in sorted(stats.items())]


def make_clip(plan, clip, all_words, out_dir, fast=False, analyze_only=False):
    t_start = time.time()
    cid = clip['id']
    opts = dict(DEFAULTS)
    opts.update(plan.get('options', {}))
    opts.update(clip.get('options', {}))
    if fast:
        opts['x264_preset'] = 'veryfast'
    src = Path(plan['video']['source'])
    work = out_dir / '_work' / cid
    work.mkdir(parents=True, exist_ok=True)
    segments = [[to_sec(a), to_sec(b)] for a, b in clip['segments']]
    seg_words = [w for s, e in segments for w in words_in_range(all_words, s, e)]
    apply_corrections(seg_words, {**plan.get('corrections', {}), **clip.get('corrections', {})})

    timings = {}
    tick = time.time()

    def lap(name):
        nonlocal tick
        timings[name] = round(time.time() - tick, 1)
        tick = time.time()

    print(f'[{cid}] 1/6 cutting segments + trimming silence...', flush=True)
    prep = prepare.prepare(src, segments, seg_words, work, trim_silence=opts['trim_silence'])
    tm = prep['timemap']
    words = remap_words(seg_words, tm)
    duration = sum(b - a for a, b, _ in tm)
    emphasis = []
    for ev in clip.get('emphasis', []):
        t = remap_time(to_sec(ev['t']), tm)
        end = remap_time(to_sec(ev['end']), tm) if ev.get('end') is not None else t + 1.5
        emphasis.append({'t': t, 'end': end, 'strength': ev.get('strength', 'medium')})

    lap('prepare')
    print(f'[{cid}] 2/6 detecting faces, shots and active speaker...', flush=True)
    fjson = work / 'faces.json'
    fdata = None
    if fjson.exists() and fjson.stat().st_mtime > Path(prep['path']).stat().st_mtime:
        fdata = json.loads(fjson.read_text(encoding='utf-8'))
        if fdata.get('version') != faces_mod.VERSION:
            fdata = None
    if fdata is None:
        fdata = faces_mod.analyze(prep['path'], fjson)
    fps = fdata['video']['fps']

    lap('faces')
    people = people_sheet(fdata, prep['path'], work / 'people.png')
    if analyze_only:
        print(f'[{cid}] people sheet: {work / "people.png"}')
        for p in people:
            print(f"   P{p['person']}: seen in {p['shots']} shot(s), mean x {p['mean_x']} px, mouth activity median {p['act_median']}")
        return None
    # speaker overrides: "main_speaker": P  and/or  "focus": [{"from","to","person": P or [P,...]}]  (source time)
    focus = []
    if clip.get('main_speaker') is not None:
        ms = clip['main_speaker']
        focus.append((0.0, duration + 1, ms if isinstance(ms, list) else [ms]))
    for fr in clip.get('focus', []):
        ps = fr['person'] if isinstance(fr['person'], list) else [fr['person']]
        focus.insert(0, (remap_time(to_sec(fr['from']), tm), remap_time(to_sec(fr['to']), tm), ps))
    opts['focus'] = focus
    print(f'[{cid}] 3/6 planning camera, zooms and SFX...', flush=True)
    cam = camera.plan(fdata, words, emphasis, duration, fps, prep['cut_points'], opts)
    opts.pop('focus', None)

    print(f'[{cid}] 4/6 building animated captions...', flush=True)
    accent, accent_why = captions.pick_accent(prep['path'], opts.get('caption_color'))
    opts['accent'] = accent
    ass_path = work / 'captions.ass'
    cap = captions.build_ass(words, ass_path, opts, highlight=clip.get('highlight_words', []),
                             split_spans=cam['split_spans'])

    lap('plan+captions')
    print(f'[{cid}] 5/6 rendering 1080x1920 (this is the slow part)...', flush=True)
    clips_dir, meta_dir, thumb_dir = out_dir / 'clips', out_dir / 'metadata', out_dir / 'thumbnails'
    for d in (clips_dir, meta_dir, thumb_dir):
        d.mkdir(parents=True, exist_ok=True)
    out_mp4 = clips_dir / f'{cid}.mp4'
    sfx_paths = sfx_mod.get_sfx_paths() if (opts['sfx'] and cam['sfx']) else {}
    render.render(prep['path'], cam, ass_path, out_mp4, fps, duration, cam['sfx'] if opts['sfx'] else [],
                  sfx_paths, opts, work)

    lap('render')
    print(f'[{cid}] 6/6 thumbnail + quality checks...', flush=True)
    strongest = sorted(emphasis, key=lambda e: {'strong': 0, 'medium': 1, 'subtle': 2}[e['strength']])
    thumb_t = min(duration - 0.1, strongest[0]['t'] + 0.4) if strongest else duration / 3
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{thumb_t:.2f}', '-i', str(out_mp4),
                    '-frames:v', '1', '-q:v', '2', str(thumb_dir / f'{cid}.jpg')], stdin=subprocess.DEVNULL)
    q = qc.check(out_mp4, duration, cam['framing_ok_ratio'], cap['groups'], len(cam['sfx']) if opts['sfx'] else 0, work)

    lap('qc')
    src_ranges = [[round(a, 2), round(b, 2)] for a, b in segments]
    meta = {
        'id': cid, 'title': clip.get('title'), 'score': clip.get('score'), 'hook': clip.get('hook'),
        'duration_sec': round(duration, 2),
        'source': {'title': plan['video'].get('title'), 'url': plan['video'].get('url'),
                   'segments_sec': src_ranges,
                   'segments_display': [f'{fmt(a)}-{fmt(b)}' for a, b in segments]},
        'description': clip.get('description'), 'social_caption': clip.get('social_caption'),
        'hashtags': clip.get('hashtags', []),
        'files': {'video': str(out_mp4), 'thumbnail': str(thumb_dir / f'{cid}.jpg')},
        'editing': {
            'accent_color': accent, 'accent_reason': accent_why,
            'highlighted_words': cap['highlighted'], 'caption_groups': cap['groups'],
            'zoom_moments': [{'t': round(e['t'], 2), 'strength': e['strength']} for e in emphasis],
            'sfx': cam['sfx'] if opts['sfx'] else [], 'split_screen_spans': cam['split_spans'],
            'jump_cuts_masked': len(cam['jump_cuts']), 'silence_removed_sec': prep['removed_silence_sec'],
            'face_detector': fdata['detector'], 'shots': len(fdata['shots']),
            'people_identified': cam['persons'], 'speaker_turns': cam['turns'],
            'options': {k: v for k, v in opts.items() if k != 'accent'},
        },
        'qc': q,
        'render_seconds': round(time.time() - t_start, 1),
        'stage_seconds': timings,
    }
    (meta_dir / f'{cid}.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    status = 'PASSED' if q['passed'] else 'NEEDS REVIEW: ' + ', '.join(k for k, v in q['checks'].items() if not v)
    print(f'[{cid}] done in {meta["render_seconds"]}s  ->  {out_mp4}  [{status}]', flush=True)
    return meta


def fmt(s):
    m, sec = divmod(s, 60)
    h, m = divmod(int(m), 60)
    return f'{h}:{m:02d}:{sec:05.2f}' if h else f'{m:02d}:{sec:05.2f}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', required=True)
    ap.add_argument('--only', help='comma-separated clip ids')
    ap.add_argument('--fast', action='store_true', help='faster, slightly larger encode')
    ap.add_argument('--keep-work', action='store_true')
    ap.add_argument('--analyze-only', action='store_true',
                    help='cut + face analysis only; writes _work/<id>/people.png for choosing main_speaker/focus')
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text(encoding='utf-8'))
    out_dir = Path(plan['output_dir'])
    out_dir.mkdir(parents=True, exist_ok=True)
    w = parse_words(plan['video']['subs'])
    if not w['precise']:
        print('NOTE: subtitles have no word-level timing; caption word times are estimated per cue.')
    only = set(a.only.split(',')) if a.only else None
    metas = []
    for clip in plan['clips']:
        if only and clip['id'] not in only:
            continue
        m = make_clip(plan, clip, w['words'], out_dir, fast=a.fast, analyze_only=a.analyze_only)
        if m:
            metas.append(m)
    if a.analyze_only:
        return
    report.write_report(out_dir, plan)
    if not a.keep_work and all(m['qc']['passed'] for m in metas):
        for m in metas:
            work = out_dir / '_work' / m['id']
            for p in work.glob('intermediate.*'):
                p.unlink(missing_ok=True)
    print(f'report: {out_dir / "clip_report.md"}')


if __name__ == '__main__':
    main()
