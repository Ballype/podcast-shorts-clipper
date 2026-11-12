"""Render the final 1080x1920 short: virtual camera + captions + audio cleanup + SFX.

Frames are decoded from the intermediate clip, transformed with sub-pixel affine
warps (smooth pans/zooms, no 1-px jitter), piped into an H.264 encoder that also
burns the ASS captions (libass) and builds the audio mix.
"""
import subprocess
from pathlib import Path

import cv2
import numpy as np

OUT_W, OUT_H = 1080, 1920


def _warp(frame, rect, out_w, out_h):
    x0, y0, cw, ch = rect
    s = out_w / cw
    sy = out_h / ch
    M = np.float32([[s, 0, -x0 * s], [0, sy, -y0 * sy]])
    return cv2.warpAffine(frame, M, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def render(intermediate, cam, ass_path, out_path, fps, duration, sfx_cues, sfx_paths, opts, work_dir):
    work_dir = Path(work_dir)
    W, H = cam['src_size']
    frames = cam['frames']
    # --- audio graph
    gains = {'whoosh': opts.get('sfx_whoosh_db', -7), 'impact': opts.get('sfx_impact_db', -8)}
    inputs = ['-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{OUT_W}x{OUT_H}', '-r', f'{fps}', '-i', '-',
              '-i', str(Path(intermediate).resolve())]
    used = [c for c in sfx_cues if c['kind'] in sfx_paths]
    for c in used:
        inputs += ['-i', str(Path(sfx_paths[c['kind']]).resolve())]
    if opts.get('keep_original_audio'):
        voice = '[1:a]aresample=48000[voice]'
    else:
        chain = ['highpass=f=70']
        if opts.get('denoise', True):
            chain.append('afftdn=nr=8:nf=-45')
        chain += ['loudnorm=I=-14:TP=-1.5:LRA=11', 'aresample=48000']
        voice = f"[1:a]{','.join(chain)}[voice]"
    fc = [voice]
    if used:
        mix_in = '[voice]'
        for k, c in enumerate(used):
            ms = int(round(c['t'] * 1000))
            fc.append(f"[{k + 2}:a]aresample=48000,volume={gains[c['kind']]}dB,adelay={ms}|{ms},apad[s{k}]")
            mix_in += f'[s{k}]'
        fc.append(f'{mix_in}amix=inputs={len(used) + 1}:normalize=0:duration=first,alimiter=limit=0.89:level=disabled[aout]')
    else:
        fc.append('[voice]alimiter=limit=0.89:level=disabled[aout]')
    ass_name = Path(ass_path).name  # relative name: avoids Windows drive-colon parsing in the filter
    fc.append(f'[0:v]ass={ass_name}[vout]')
    enc = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y', *inputs,
           '-filter_complex', ';'.join(fc), '-map', '[vout]', '-map', '[aout]',
           '-c:v', 'libx264', '-preset', opts.get('x264_preset', 'medium'), '-crf', str(opts.get('crf', 18)),
           '-pix_fmt', 'yuv420p', '-r', f'{fps}', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
           '-t', f'{duration:.3f}', '-movflags', '+faststart', str(Path(out_path).resolve())]
    dec = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(intermediate), '-an', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    pdec = subprocess.Popen(dec, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    penc = subprocess.Popen(enc, stdin=subprocess.PIPE, cwd=str(Path(ass_path).parent), stderr=subprocess.PIPE)
    size = W * H * 3
    half = OUT_H // 2
    i = 0
    broken = False
    try:
        while not broken:
            buf = pdec.stdout.read(size)
            if len(buf) < size:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            f = frames[min(i, len(frames) - 1)]
            if f['mode'] == 'split':
                top = _warp(fr, f['top'], OUT_W, half)
                bot = _warp(fr, f['bot'], OUT_W, OUT_H - half)
                out = np.vstack([top, bot])
                out[half - 3:half + 3, :] = 0  # thin divider
            else:
                out = _warp(fr, f['rect'], OUT_W, OUT_H)
            try:
                penc.stdin.write(out.tobytes())
            except OSError:
                broken = True  # encoder exited; its stderr explains why
            i += 1
    finally:
        pdec.stdout.close()
        pdec.wait()
        try:
            penc.stdin.close()
        except OSError:
            pass
        err = penc.stderr.read().decode('utf-8', 'replace')
        penc.wait()
    if penc.returncode != 0:
        raise RuntimeError(f'encode failed: {err[-2000:]}')
    return {'frames_written': i}
