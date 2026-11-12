"""Plan the 9:16 virtual camera: speaker following, smooth reframing, zooms, split screen.

Input: faces.json (faces.py), output-time words, emphasis moments, options.
Output: one crop instruction per output frame plus caption layout spans and SFX cues.
"""
import math

import numpy as np

ZOOM_LEVELS = {'subtle': 0.08, 'medium': 0.18, 'strong': 0.32}
STYLE_MULT = {'off': 0.0, 'subtle': 0.6, 'normal': 1.0, 'strong': 1.4}


def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _moving_avg(a, k):
    if len(a) == 0 or k <= 1:
        return a
    pad = k // 2
    ap = np.pad(a, (pad, pad), mode='edge')
    return np.convolve(ap, np.ones(k) / k, mode='valid')[:len(a)]


def speaking_mask(times, words, pad=0.25):
    m = np.zeros(len(times), bool)
    for w in words:
        m |= (times >= w['s'] - pad) & (times <= w['e'] + pad)
    return m


def identify_persons(samples, max_dist=0.33):
    """Group face tracks into people by clothing colour, across camera cuts.

    Tracks visible at the same moment can never be the same person."""
    import cv2
    descs, times = {}, {}
    for s in samples:
        for f in s['faces']:
            if f.get('desc'):
                descs.setdefault(f['id'], []).append(f['desc'])
            times.setdefault(f['id'], set()).add(s['t'])
    mean = {k: np.float32(np.mean(v, axis=0)) for k, v in descs.items() if len(v) >= 3}
    persons = []  # [{'tracks': [...], 'desc': hist, 'times': set}]
    track_person = {}
    for k in sorted(mean, key=lambda k: -len(descs[k])):
        best, bd = None, max_dist
        for pi, p in enumerate(persons):
            if p['times'] & times[k]:
                continue
            d = cv2.compareHist(mean[k], p['desc'], cv2.HISTCMP_BHATTACHARYYA)
            if d < bd:
                best, bd = pi, d
        if best is None:
            persons.append({'tracks': [k], 'desc': mean[k], 'times': set(times[k])})
            track_person[k] = len(persons) - 1
        else:
            persons[best]['tracks'].append(k)
            persons[best]['times'] |= times[k]
            track_person[k] = best
    return track_person


def speaker_turns(words, min_dur=1.0, min_words=3):
    """Turns from YouTube's '>>' speaker-change markers. Short interjections
    ("Yeah.", "Did it work?") are merged into the surrounding turn so the
    camera doesn't ping-pong."""
    if not any(w.get('turn') for w in words):
        return []
    turns = []
    for w in words:
        if w.get('turn') or not turns:
            turns.append([w['s'], w['e'], 1])
        else:
            turns[-1][1] = w['e']
            turns[-1][2] += 1
    merged = []
    for t in turns:
        if merged and (t[1] - t[0] < min_dur or t[2] < min_words):
            merged[-1][1] = t[1]
        else:
            merged.append(t[:2])
    return merged


def turn_speakers(turns, samples, track_person):
    """Pick the person with the highest median mouth activity in each turn.
    (Median, not mean: hand fidgeting near the mouth makes short spikes.)"""
    out = []
    for a, b in turns:
        score = {}
        for s in samples:
            if a <= s['t'] <= b:
                for f in s['faces']:
                    p = track_person.get(f['id'])
                    if p is not None:
                        score.setdefault(p, []).append(f['act'])
        score = {p: v for p, v in score.items() if len(v) >= 3}
        if score:
            spk = max(score, key=lambda p: float(np.median(score[p])))
            out.append((a, b, spk))
    return out


def plan_shot_speakers(samples, shot, words, track_person=None, turn_spk=(), focus=()):
    """Return per-sample active track id (or None) and track positions within one shot."""
    s0, s1 = shot
    ss = [s for s in samples if s0 - 0.02 <= s['t'] < s1 - 0.02]
    if not ss:
        return [], {}
    times = np.array([s['t'] for s in ss])
    ids = {}
    for i, s in enumerate(ss):
        for f in s['faces']:
            ids.setdefault(f['id'], []).append(i)
    tracks = {k: v for k, v in ids.items() if len(v) >= max(2, 0.3 * len(ss))}
    if not tracks:
        return [{'t': t, 'active': None, 'faces': {}} for t in times], {}
    act = {}
    pos = {}
    for k in tracks:
        a = np.zeros(len(ss))
        p = np.full((len(ss), 4), np.nan)
        for i, s in enumerate(ss):
            for f in s['faces']:
                if f['id'] == k:
                    a[i] = f['act']
                    p[i] = (f['cx'], f['cy'], f['w'], f['h'])
        # fill gaps in position by carrying neighbours
        for c in range(4):
            col = p[:, c]
            ok = ~np.isnan(col)
            if ok.any():
                p[:, c] = np.interp(np.arange(len(col)), np.flatnonzero(ok), col[ok])
        act[k] = _moving_avg(a, 7)
        pos[k] = p
    speaking = speaking_mask(times, words)
    keys = list(tracks)
    # start on the most active face in the first ~2 s (or the largest face if nobody moves)
    first = slice(0, max(1, min(len(ss), 12)))
    cur = max(keys, key=lambda k: (act[k][first].mean(), pos[k][0, 2]))
    held, streak, cand_prev = 0.0, 0, None
    dt = times[1] - times[0] if len(times) > 1 else 1 / 6
    out = []
    track_person = track_person or {}
    for i, t in enumerate(times):
        # 1) Claude's focus override (people sheet), 2) audio turn speaker, 3) visual heuristic
        want_track = None
        for a, b, persons in focus:
            if a <= t <= b:
                want_track = next((k for k in keys if track_person.get(k) in persons), None)
                break
        if want_track is None:
            want = next((p for a, b, p in turn_spk if a - 0.3 <= t <= b + 0.6), None)
            want_track = next((k for k in keys if track_person.get(k) == want), None) if want is not None else None
        if want_track is not None:
            # audio-derived turn says who is talking and that person is on screen
            if want_track != cur:
                cur, held, streak = want_track, 0.0, 0
        elif speaking[i] and len(keys) > 1:
            cand = max(keys, key=lambda k: act[k][i])
            if cand != cur and act[cand][i] > act[cur][i] * 1.25 + 0.3:
                streak = streak + 1 if cand == cand_prev else 1
                cand_prev = cand
                if streak >= 4 and held >= 1.0:
                    cur, held, streak = cand, 0.0, 0
            else:
                streak = 0
        held += dt
        out.append({'t': float(t), 'active': cur,
                    'faces': {k: tuple(float(v) for v in pos[k][i]) for k in keys}})
    return out, {k: float(np.median(pos[k][:, 2])) for k in keys}


def emphasis_curve(t, events, mult):
    z = 0.0
    for ev in events:
        lvl = ZOOM_LEVELS.get(ev.get('strength', 'medium'), 0.18) * mult
        t0, t1 = ev['t'], max(ev.get('end', ev['t'] + 1.5), ev['t'] + 0.6)
        if t < t0 - 0.3 or t > t1 + 0.55:
            continue
        if t < t0 + 0.15:
            v = _ease((t - (t0 - 0.3)) / 0.45)
        elif t <= t1:
            v = 1.0
        else:
            v = 1.0 - _ease((t - t1) / 0.55)
        z = max(z, lvl * v)
    return z


def hook_curve(t, mult):
    if mult == 0:
        return 0.0
    if t < 2.0:
        return 0.07 * mult * _ease(t / 2.0)
    if t < 3.0:
        return 0.07 * mult
    return 0.07 * mult * (1 - _ease((t - 3.0) / 0.8))


def plan(faces, words, emphasis, duration, fps, cut_points, opts):
    W, H = faces['video']['w'], faces['video']['h']
    mult = STYLE_MULT.get(opts.get('zoom', 'normal'), 1.0)
    split_opt = opts.get('split_screen', 'auto')
    crop_w1 = H * 9 / 16
    shots = faces['shots'] or [[0.0, duration]]
    shot_cuts = [s[0] for s in shots[1:]]
    # who is who across cuts, and who speaks in each audio turn
    track_person = identify_persons(faces['samples'])
    turns = speaker_turns(words)
    turn_spk = turn_speakers(turns, faces['samples'], track_person) if turns else []
    # per-shot speaker timelines
    shot_plans = []
    for sh in shots:
        timeline, widths = plan_shot_speakers(faces['samples'], sh, words, track_person, turn_spk,
                                              opts.get('focus', ()))
        shot_plans.append((sh, timeline, widths))

    # split-screen spans: rapid back-and-forth between two visible people
    split_spans = []
    # auto split needs audio speaker turns: visual-only switching is too noisy to trust
    if split_opt == 'always' or (split_opt == 'auto' and turns):
        for sh, tl, widths in shot_plans:
            if len(widths) < 2 or not tl:
                continue
            sw = [i for i in range(1, len(tl)) if tl[i]['active'] != tl[i - 1]['active']]
            sw_t = [tl[i]['t'] for i in sw]
            for i, t in enumerate(sw_t):
                win = [x for x in sw_t if t <= x <= t + 8.0]
                if split_opt == 'always' or len(win) >= 3:
                    a, b = max(sh[0], t - 0.5), min(sh[1], win[-1] + 1.5)
                    if b - a >= 2.5:
                        split_spans.append([a, b])
        merged = []
        for a, b in sorted(split_spans):
            if merged and a <= merged[-1][1] + 1.0:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        split_spans = merged

    jump_cuts = [c for c in cut_points if all(abs(c - s) > 0.15 for s in shot_cuts)]
    n = int(math.ceil(duration * fps))
    frames = []
    cam = {}
    tau = opts.get('pan_smoothing', 0.15)
    alpha = 1 - math.exp(-(1 / fps) / tau)
    prev_shot = None
    prev_active = None
    framing_total = framing_good = 0
    for i in range(n):
        t = i / fps
        si = max(0, sum(1 for c in shot_cuts if c <= t))
        sh, tl, widths = shot_plans[min(si, len(shot_plans) - 1)]
        snap = si != prev_shot
        prev_shot = si
        # nearest timeline sample
        state = None
        if tl:
            j = min(range(len(tl)), key=lambda k: abs(tl[k]['t'] - t)) if len(tl) < 3 else \
                int(min(len(tl) - 1, max(0, round((t - tl[0]['t']) * faces['sample_fps']))))
            state = tl[j]
        in_split = any(a <= t < b for a, b in split_spans)
        zoom_extra = emphasis_curve(t, emphasis, mult) + hook_curve(t, mult)
        jc = sum(1 for c in jump_cuts if c <= t)
        if mult > 0 and jc % 2 == 1:
            zoom_extra += 0.06
        if in_split and state and len(state['faces']) >= 2:
            ids = sorted(state['faces'], key=lambda k: state['faces'][k][0])
            act_sorted = ids[:2] if len(ids) == 2 else sorted(ids, key=lambda k: -widths.get(k, 0))[:2]
            act_sorted = sorted(act_sorted, key=lambda k: state['faces'][k][0])
            rects = []
            for slot, k in zip(('top', 'bot'), act_sorted):
                cx, cy, fw, fh = state['faces'][k]
                ch = min(H, fh * 3.2 / (1 + zoom_extra * 0.5))
                cw = min(W, ch * 9 / 8)
                ch = cw * 8 / 9
                key = slot
                if snap or key not in cam or prev_active != 'split':
                    cam[key] = [cx, cy]
                cam[key][0] += (cx - cam[key][0]) * alpha
                cam[key][1] += (cy - cam[key][1]) * alpha
                x0 = min(max(0, cam[key][0] - cw / 2), W - cw)
                y0 = min(max(0, cam[key][1] - 0.45 * ch), H - ch)
                rects.append([round(x0, 2), round(y0, 2), round(cw, 2), round(ch, 2)])
            frames.append({'mode': 'split', 'top': rects[0], 'bot': rects[1]})
            prev_active = 'split'
            continue
        if state and state['active'] is not None:
            cx, cy, fw, fh = state['faces'][state['active']]
            med_w = np.median(list(widths.values())) if widths else fw
            zb = min(max(crop_w1 / (5.0 * med_w), 1.0), opts.get('max_base_zoom', 1.4))
            zmax = max(1.0, crop_w1 / (2.6 * fw))
        else:
            cx, cy, fw = W / 2, H / 2, 0
            zb, zmax = 1.0, 1.5
        z = min(zb * (1 + zoom_extra), max(zmax, zb))
        ch = H / z
        cw = ch * 9 / 16
        if cw > W:
            cw, ch = W, W * 16 / 9
        key = 'single'
        if snap or key not in cam or prev_active != 'single':
            cam[key] = [cx, cy, cx, cy]  # current x, y, deadzone target x, y
        c = cam[key]
        if abs(cx - c[2]) > 0.07 * cw or (state and state['active'] != cam.get('who')):
            c[2] = cx
        if abs(cy - c[3]) > 0.06 * ch:
            c[3] = cy
        cam['who'] = state['active'] if state else None
        c[0] += (c[2] - c[0]) * alpha
        c[1] += (c[3] - c[1]) * alpha
        x0 = min(max(0, c[0] - cw / 2), W - cw)
        y0 = min(max(0, c[1] - 0.38 * ch), H - ch)
        if state and state['active'] is not None:
            framing_total += 1
            # face fully inside the crop with a small margin (head not cut off)
            face_l, face_r, face_t = cx - 0.5 * fw, cx + 0.5 * fw, cy - 0.6 * fh
            if face_l >= x0 and face_r <= x0 + cw and face_t >= y0 - 2:
                framing_good += 1
        frames.append({'mode': 'single', 'rect': [round(x0, 2), round(y0, 2), round(cw, 2), round(ch, 2)]})
        prev_active = 'single'

    # SFX cues from zoom moments (never denser than one per 2 s)
    sfx = []
    if opts.get('sfx', True) and mult > 0:
        for ev in sorted(emphasis, key=lambda e: e['t']):
            st = ev.get('strength', 'medium')
            if st == 'subtle':
                continue
            cues = [('whoosh', ev['t'] - 0.32)]
            if st == 'strong':
                cues.append(('impact', ev['t'] + 0.05))
            for kind, ct in cues:
                if ct < 0:
                    continue
                if sfx and ct - sfx[-1]['t'] < 2.0 and kind == 'whoosh':
                    continue
                sfx.append({'kind': kind, 't': round(ct, 3)})
    return {'frames': frames, 'split_spans': split_spans, 'sfx': sfx, 'jump_cuts': jump_cuts,
            'persons': len(set(track_person.values())), 'turns': len(turns),
            'turn_speakers': [[round(a, 2), round(b, 2), p] for a, b, p in turn_spk],
            'src_size': [W, H], 'framing_ok_ratio': framing_good / framing_total if framing_total else 1.0}
