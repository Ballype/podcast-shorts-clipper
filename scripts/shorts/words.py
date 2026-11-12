"""Word-level timestamps from subtitle files.

YouTube auto-captions (VTT) carry a timestamp for every word inside <c> tags,
so we get real word timing without running a speech model. For subtitles that
only have cue-level timing (manual uploads, SRT), word times are estimated by
spreading each cue across its words by character length; that fallback is
less precise and is reported as such.

Usage (debug): python words.py <subs.vtt> [start_sec end_sec]
"""
import html
import json
import re
import sys
from pathlib import Path

TS = r'(\d+:)?\d\d:\d\d[.,]\d+'
CUE_RE = re.compile(rf'({TS})\s*-->\s*({TS})')
WORD_TAG_RE = re.compile(r'<(\d+:\d\d:\d\d\.\d+)>')
FILLERS = {'uh', 'um', 'uhm', 'erm', 'mhm', 'hmm'}


def _ts(s: str) -> float:
    s = s.replace(',', '.')
    parts = s.split(':')
    if len(parts) == 2:
        parts = ['0'] + parts
    h, m, r = parts
    return int(h) * 3600 + int(m) * 60 + float(r)


def _clean_token(tok: str) -> str:
    tok = html.unescape(tok).replace('\xa0', ' ').strip()
    tok = tok.replace('>>', '').strip()
    return tok


def _split_censor(text: str) -> str:
    # YouTube renders bleeped words as "[ __ ]"
    return re.sub(r'\[\s*__\s*\]', '****', text)


def parse_words(sub_path: str) -> dict:
    """Return {"words": [{"w","s","e"}], "precise": bool}."""
    raw = Path(sub_path).read_text(encoding='utf-8', errors='replace')
    raw = _split_censor(raw)
    blocks = re.split(r'\n\s*\n', raw)
    words, precise_found = [], False
    pending_turn = False
    cue_level = []  # (start, end, text) for the fallback path
    untagged_new = []  # single-word lines YouTube writes without word tags
    for b in blocks:
        m = CUE_RE.search(b)
        if not m:
            continue
        c_start, c_end = _ts(m.group(1)), _ts(m.group(3))
        body = [l for l in b.split('\n') if l.strip() and '-->' not in l and not l.strip().isdigit()]
        tagged = [l for l in body if WORD_TAG_RE.search(l)]
        if tagged:
            precise_found = True
            for line in tagged:
                # first word starts at cue start; each <ts> marks the next word's start
                pieces = WORD_TAG_RE.split(line)
                t = c_start
                for i, piece in enumerate(pieces):
                    if i % 2 == 1:
                        t = _ts(piece)
                        continue
                    text = html.unescape(re.sub(r'</?c[^>]*>', '', piece)).replace('\xa0', ' ')
                    # ">>" is YouTube's speaker-change marker (from the audio): keep it as a turn flag
                    j = 0
                    for tok in text.split():
                        turn = tok.startswith('>>')
                        tok = tok.replace('>>', '').strip()
                        if not tok:
                            pending_turn = True
                            continue
                        words.append({'w': tok, 's': t + j * 0.05, 'e': None, 'cue_end': c_end,
                                      'turn': turn or pending_turn})
                        pending_turn = False
                        j += 1
        else:
            # untagged text in a word-timed file is usually a rolling duplicate; keep it for the fallback
            text = _clean_token(re.sub(r'<[^>]+>', '', ' '.join(body)))
            if text:
                cue_level.append((c_start, c_end, text))
            # ...except a 2-line cue whose new (second) line has no tags: YouTube writes
            # single-word lines that way, e.g. "paycheck". It starts at the cue start.
            if len(body) >= 2 and c_end - c_start > 0.05:
                new_line = html.unescape(re.sub(r'<[^>]+>', '', body[-1])).replace('\xa0', ' ').strip()
                prev_line = html.unescape(re.sub(r'<[^>]+>', '', body[-2])).replace('\xa0', ' ').strip()
                if new_line and new_line != prev_line:
                    untagged_new.append((c_start, c_end, new_line))

    if not precise_found:
        words = []
        last_text = None
        for c_start, c_end, text in cue_level:
            if text == last_text:
                continue
            last_text = text
            toks = text.split()
            total = sum(len(t) + 1 for t in toks) or 1
            t = c_start
            for tok in toks:
                d = (c_end - c_start) * (len(tok) + 1) / total
                words.append({'w': tok, 's': t, 'e': t + d})
                t += d
    else:
        for c_start, c_end, line in untagged_new:
            toks = line.split()
            turn = False
            for j, tok in enumerate(toks):
                if tok.startswith('>>'):
                    turn = True
                    tok = tok.replace('>>', '').strip()
                    if not tok:
                        continue
                step = min(0.3, (c_end - c_start) / max(1, len(toks)))
                words.append({'w': tok, 's': c_start + j * step, 'e': None, 'cue_end': c_end, 'turn': turn})
                turn = False
        words.sort(key=lambda w: w['s'])
        for i, w in enumerate(words):
            nxt = words[i + 1]['s'] if i + 1 < len(words) else w['cue_end']
            end = min(nxt, w['s'] + 1.0)
            if end <= w['s']:
                end = w['s'] + 0.2
            w['e'] = end
            w.pop('cue_end', None)
    words = [w for w in words if w['w']]
    # bleeps can arrive split across word tags as "[", "__", "]"
    # Sound tags YouTube inserts ([laughter], [music], multi-word [clears throat]) are not speech: drop them,
    # so they never show up in captions. A bleep "[ __ ]" can have such a tag wedged inside it.
    cleaned, in_tag = [], False
    for w in words:
        t = w['w']
        if in_tag:
            in_tag = not t.endswith(']')
            continue
        if re.fullmatch(r'\[[A-Za-z][^\]]*\]', t):
            continue
        if re.fullmatch(r'\[[A-Za-z].*', t):
            in_tag = True
            continue
        cleaned.append(w)
    words = cleaned
    merged, i = [], 0
    while i < len(words):
        if words[i]['w'] == '[' and i + 2 < len(words) and words[i + 1]['w'] == '__' and words[i + 2]['w'].startswith(']'):
            m = dict(words[i])
            m['w'] = '****' + words[i + 2]['w'][1:]
            m['e'] = words[i + 2]['e']
            merged.append(m)
            i += 3
            continue
        if words[i]['w'] in ('[', ']', '__'):  # stray pieces of a broken bleep
            if words[i]['w'] == '__':
                m = dict(words[i]); m['w'] = '****'; merged.append(m)
            i += 1
            continue
        merged.append(words[i])
        i += 1
    return {'words': merged, 'precise': precise_found}


def words_in_range(words, start, end):
    return [dict(w) for w in words if w['s'] >= start - 0.05 and w['s'] < end]


def remap_words(words, timemap):
    """timemap: list of (src_start, src_end, out_start). Drops words in removed gaps."""
    out = []
    for w in words:
        for s0, s1, o0 in timemap:
            if s0 - 0.05 <= w['s'] < s1:
                nw = dict(w)
                nw['src_s'] = w['s']
                nw['s'] = max(0.0, o0 + (w['s'] - s0))
                nw['e'] = o0 + (min(w['e'], s1) - s0)
                if nw['e'] <= nw['s']:
                    nw['e'] = nw['s'] + 0.15
                out.append(nw)
                break
    return out


def remap_time(t, timemap):
    """Map a source timestamp into output time (snaps to nearest kept segment)."""
    best = None
    for s0, s1, o0 in timemap:
        if s0 <= t <= s1:
            return o0 + (t - s0)
        d = min(abs(t - s0), abs(t - s1))
        cand = o0 if t < s0 else o0 + (s1 - s0)
        if best is None or d < best[0]:
            best = (d, cand)
    return best[1] if best else 0.0


if __name__ == '__main__':
    res = parse_words(sys.argv[1])
    ws = res['words']
    if len(sys.argv) >= 4:
        ws = words_in_range(ws, float(sys.argv[2]), float(sys.argv[3]))
    print(json.dumps({'precise': res['precise'], 'count': len(ws)}))
    for w in ws[:60]:
        print(f"{w['s']:9.2f} {w['e']:9.2f} {w['w']}")
