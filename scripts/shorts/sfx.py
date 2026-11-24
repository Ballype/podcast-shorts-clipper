"""Sound effects for zoom moments.

The skill ships with no licensed SFX library, so default effects are synthesised
here (filtered-noise whoosh, low soft impact). To use your own sounds, drop WAV
files named whoosh.wav / impact.wav into assets/sfx/custom/ and they take priority.
"""
import math
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
SFX_DIR = REPO / 'assets' / 'sfx'
SR = 48000


def _write(path, x):
    x = np.clip(x, -1, 1)
    data = (x * 32767).astype(np.int16)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def _whoosh(dur=0.5, seed=7):
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    noise = rng.standard_normal(n)
    t = np.arange(n) / SR
    # sweep a resonant band-pass from low to high (simple 2-pole filter per sample)
    f = 300 + 3200 * (t / dur) ** 1.5
    y = np.zeros(n)
    y1 = y2 = 0.0
    q = 2.2
    for i in range(n):
        w0 = 2 * math.pi * f[i] / SR
        a = math.sin(w0) / (2 * q)
        b0, a0, a1, a2 = a, 1 + a, -2 * math.cos(w0), 1 - a
        v = (b0 * noise[i] - a1 * y1 - a2 * y2) / a0
        y2, y1 = y1, v
        y[i] = v
    env = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 2
    y = y * env
    return y / (np.abs(y).max() + 1e-9) * 0.9


def _impact(dur=0.45):
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 95 * np.exp(-t * 6) + 45
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * np.exp(-t * 9)
    click = np.random.default_rng(3).standard_normal(n) * np.exp(-t * 120) * 0.25
    y = body + click
    return y / (np.abs(y).max() + 1e-9) * 0.9


def get_sfx_paths():
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    custom = SFX_DIR / 'custom'
    out = {}
    for name, gen in (('whoosh', _whoosh), ('impact', _impact)):
        c = custom / f'{name}.wav'
        if c.exists():
            out[name] = c
            continue
        p = SFX_DIR / f'{name}.wav'
        if not p.exists():
            _write(p, gen())
        out[name] = p
    chime_path = SFX_DIR / 'chime.wav'
    if chime_path.exists():
        out['chime'] = chime_path
    return out


if __name__ == '__main__':
    print(get_sfx_paths())
