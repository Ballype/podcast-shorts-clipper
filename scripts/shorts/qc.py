"""Automated quality checks on a rendered short + a contact sheet for visual review."""
import json
import re
import subprocess
from pathlib import Path


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)


def probe(path):
    r = _run(['ffprobe', '-v', 'error', '-show_entries',
              'stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate:format=duration', '-of', 'json', str(path)])
    return json.loads(r.stdout)


def loudness_and_black(path):
    """One decode pass: integrated loudness + true peak (EBU R128) and black-frame detection."""
    r = _run(['ffmpeg', '-nostdin', '-hide_banner', '-i', str(path), '-af', 'ebur128=peak=true',
              '-vf', 'scale=270:-2,blackdetect=d=0.08:pix_th=0.08', '-f', 'null', '-'])
    tail = r.stderr[r.stderr.rfind('Summary:'):]
    i = re.search(r'I:\s+(-?[\d.]+) LUFS', tail)
    p = re.search(r'Peak:\s+(-?[\d.]+) dBFS', tail)
    blacks = re.findall(r'black_start:([\d.]+) black_end:([\d.]+)', r.stderr)
    return (float(i.group(1)) if i else None, float(p.group(1)) if p else None, blacks)


def contact_sheet(path, out_png, duration, n=8):
    times = [duration * (k + 0.5) / n for k in range(n)]
    tiles = []
    for k, t in enumerate(times):
        tp = Path(out_png).with_name(f'_tile{k}.png')
        _run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', str(path), '-frames:v', '1',
              '-vf', 'scale=270:480', str(tp)])
        tiles.append(tp)
    ins = []
    for tp in tiles:
        ins += ['-i', str(tp)]
    layout = '|'.join(f'{(k % 4) * 270}_{(k // 4) * 480}' for k in range(n))
    _run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *ins, '-filter_complex', f'xstack=inputs={n}:layout={layout}', str(out_png)])
    for tp in tiles:
        tp.unlink(missing_ok=True)
    return [round(t, 2) for t in times]


def check(path, expected_duration, framing_ok_ratio, caption_groups, sfx_count, work_dir):
    j = probe(path)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    a = next((s for s in j['streams'] if s['codec_type'] == 'audio'), None)
    dur = float(j['format']['duration'])
    lufs, peak, blacks = loudness_and_black(path)
    sheet = Path(work_dir) / 'contact_sheet.png'
    times = contact_sheet(path, sheet, dur)
    checks = {
        'resolution_1080x1920': v['width'] == 1080 and v['height'] == 1920,
        'codec_h264_aac': v['codec_name'] == 'h264' and a is not None and a['codec_name'] == 'aac',
        'duration_matches': abs(dur - expected_duration) < 0.25,
        'no_black_frames': len(blacks) == 0,
        'loudness_ok': lufs is not None and -16.5 <= lufs <= -11.5,
        'no_clipping': peak is not None and peak <= -0.5,
        'speaker_framed': framing_ok_ratio >= 0.9,
        'captions_present': caption_groups > 0,
    }
    return {
        'checks': checks,
        'passed': all(checks.values()),
        'measured': {'duration': round(dur, 2), 'fps': v['r_frame_rate'], 'integrated_lufs': lufs,
                     'peak_dbfs': peak, 'black_segments': blacks, 'framing_ok_ratio': round(framing_ok_ratio, 3),
                     'caption_groups': caption_groups, 'sfx_count': sfx_count},
        'contact_sheet': str(sheet), 'contact_sheet_times': times,
    }
