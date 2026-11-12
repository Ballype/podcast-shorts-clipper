"""Animated word-level captions as an ASS subtitle file (rendered by libass in FFmpeg).

- 2-3 words per caption, broken at punctuation and pauses
- each word appears exactly when it is spoken (later words stay invisible, so the
  line never reflows), each new phrase pops in with a quick scale animation
- selected words are highlighted in an accent colour
"""
import re

import cv2
import numpy as np

DEFAULT_ACCENT = '#FFD400'  # clean, high-contrast yellow
MONEY_RE = re.compile(r'^(\$|£|€)|\d|^(million|millions|billion|billions|thousand|grand|hundred|percent|k)$', re.I)


def hex_to_ass(hex_color, alpha=0):
    h = hex_color.lstrip('#')
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f'&H{alpha:02X}{b}{g}{r}'.upper()


def _norm(w):
    return re.sub(r"[^\w$£€%']", '', w.lower())


def _display(w):
    """Caption text for a word: drop quotes/commas/full stops, keep ? ! and apostrophes."""
    w = w.replace('{', '(').replace('}', ')')
    w = re.sub(r'["“”„«»]', '', w)
    w = re.sub(r'[.,;:]+$', '', w)
    w = re.sub(r'^[\'‘’]+|[\'‘’]+$', '', w) if not re.search(r"\w'\w", w) else w
    return w or '…'


def pick_accent(video_path, user_color=None, samples=8):
    """User colour > readable brand/accent colour from the footage > yellow."""
    if user_color:
        return user_color, 'user-specified'
    cap = cv2.VideoCapture(str(video_path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    hsv_all = []
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / samples))
        ok, fr = cap.read()
        if ok:
            hsv_all.append(cv2.cvtColor(cv2.resize(fr, (320, 180)), cv2.COLOR_BGR2HSV).reshape(-1, 3))
    cap.release()
    if not hsv_all:
        return DEFAULT_ACCENT, 'no frames read; default yellow'
    hsv = np.concatenate(hsv_all)
    vivid = hsv[(hsv[:, 1] > 110) & (hsv[:, 2] > 110)]
    if len(vivid) < 0.02 * len(hsv):
        return DEFAULT_ACCENT, 'footage has no strong accent colour; default yellow'
    hist = np.bincount(vivid[:, 0] // 6, minlength=30)
    hue = int((np.argmax(hist) * 6 + 3))
    share = hist.max() / len(hsv)
    # A colour covering a large share of the frame is set lighting / background:
    # highlighted words in that hue would blend in, so fall back to yellow.
    if share > 0.04:
        return DEFAULT_ACCENT, f'dominant footage hue ({hue * 2}°) is background lighting ({share:.0%} of frame); default yellow for contrast'
    bgr = cv2.cvtColor(np.uint8([[[hue, 200, 255]]]), cv2.COLOR_HSV2BGR)[0, 0]
    hex_c = '#{:02X}{:02X}{:02X}'.format(int(bgr[2]), int(bgr[1]), int(bgr[0]))
    return hex_c, f'accent extracted from footage (hue {hue * 2}°)'


def group_words(words, max_words=3, max_chars=14, gap=0.45):
    groups, cur = [], []
    for i, w in enumerate(words):
        if cur:
            prev = cur[-1]
            text_len = sum(len(x['w']) + 1 for x in cur) + len(w['w'])
            if (len(cur) >= max_words or text_len > max_chars or re.search(r'[.?!,;:]$', prev['w'])
                    or w['s'] - prev['e'] > gap):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def build_ass(words, out_path, opts, highlight=(), split_spans=(), drop_fillers=True):
    size = int(round(104 * float(opts.get('caption_size', 1.0))))
    accent = hex_to_ass(opts['accent'])
    font = opts.get('caption_font', 'Arial Black')
    pos_name = opts.get('caption_position', 'lower')
    y_default = {'lower': 1300, 'middle': 960, 'upper': 520}.get(pos_name, 1300)
    upper = opts.get('caption_uppercase', True)
    hl = {_norm(h) for h in highlight}
    fillers = {'uh', 'um', 'uhm', 'erm', 'mhm', 'hmm'}
    ws = [w for w in words if not (drop_fillers and _norm(w['w']) in fillers)]
    groups = group_words(ws, max_words=int(opts.get('words_per_caption', 3)))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,7,4,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    highlighted = []
    for gi, g in enumerate(groups):
        g_end = g[-1]['e'] + 0.25
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0]['s'])
        g_end = max(g_end, g[-1]['s'] + 0.2)
        mid = (g[0]['s'] + g_end) / 2
        y = 960 if any(a <= mid < b for a, b in split_spans) else y_default
        for k, w in enumerate(g):
            st = w['s']
            en = g[k + 1]['s'] if k + 1 < len(g) else g_end
            if en - st < 0.04:
                continue
            parts = []
            for j, x in enumerate(g):
                txt = _display(x['w'])
                txt = txt.upper() if upper else txt
                is_hl = _norm(x['w']) in hl or bool(MONEY_RE.search(_norm(x['w'])))
                if is_hl and j == k:
                    highlighted.append(x['w'])
                col = f'\\c{accent}' if is_hl else '\\c&H00FFFFFF&'
                if j < k:
                    parts.append(f'{{{col}\\alpha&H00&}}{txt}')
                elif j == k:
                    parts.append(f'{{{col}\\alpha&HFF&\\t(0,70,\\alpha&H00&)}}{txt}')
                else:
                    parts.append(f'{{\\alpha&HFF&}}{txt}')
            pop = '\\fscx82\\fscy82\\t(0,90,\\fscx106\\fscy106)\\t(90,150,\\fscx100\\fscy100)' if k == 0 else ''
            text = f'{{\\an5\\pos(540,{y}){pop}}}' + ' '.join(parts)
            events.append([st, en, text])
    # never let two caption events overlap (they would draw on top of each other)
    events.sort(key=lambda e: e[0])
    for i in range(len(events) - 1):
        events[i][1] = min(events[i][1], events[i + 1][0])
    lines = [f'Dialogue: 0,{_ts(st)},{_ts(en)},Cap,,0,0,0,,{text}' for st, en, text in events if en - st >= 0.03]
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(lines) + '\n')
    return {'groups': len(groups), 'highlighted': highlighted}
