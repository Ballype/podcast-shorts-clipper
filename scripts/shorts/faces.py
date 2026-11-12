"""Face detection, tracking, shot detection and active-speaker estimation.

- Shots: ffmpeg scene detection at full frame rate (precise cut times).
- Faces: OpenCV YuNet detector (assets/models/face_detection_yunet_2023mar.onnx),
  sampled at SAMPLE_FPS on a downscaled copy; falls back to a Haar cascade if the
  model file is missing.
- Active speaker: mouth-region motion (frame difference around the mouth landmarks)
  minus general head motion, smoothed over time. This is a visual heuristic, not an
  audio-visual model: it works well for seated podcasts, less well for profiles,
  covered mouths or heavy gesturing.
"""
import json
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

SAMPLE_FPS = 6
VERSION = 3  # bump when the faces.json format changes (invalidates caches)
ANALYSIS_W = 960
REPO = Path(__file__).resolve().parents[2]
YUNET = REPO / 'assets' / 'models' / 'face_detection_yunet_2023mar.onnx'


def probe(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height,r_frame_rate:format=duration', '-of', 'json', str(path)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    j = json.loads(r.stdout)
    st = j['streams'][0]
    num, den = st['r_frame_rate'].split('/')
    return {'w': int(st['width']), 'h': int(st['height']), 'fps': float(num) / float(den),
            'duration': float(j['format']['duration'])}


def detect_shots(path, threshold=0.32):
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-i', str(path), '-an', '-vf',
           f"scale=320:-2,select='gt(scene,{threshold})',showinfo", '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
    cuts = [float(m.group(1)) for m in re.finditer(r'pts_time:([\d.]+)', r.stderr)]
    return sorted(set(round(c, 3) for c in cuts))


def iter_frames(path, fps, w, h):
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-an', '-vf', f'fps={fps},scale={w}:{h}',
           '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    size = w * h * 3
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        p.stdout.close()
        p.wait()


class Detector:
    def __init__(self, w, h):
        self.kind = 'yunet' if YUNET.exists() else 'haar'
        if self.kind == 'yunet':
            self.det = cv2.FaceDetectorYN.create(str(YUNET), '', (w, h), score_threshold=0.7, nms_threshold=0.3, top_k=20)
        else:
            self.det = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

    def __call__(self, img):
        out = []
        if self.kind == 'yunet':
            _, faces = self.det.detect(img)
            if faces is None:
                return out
            for f in faces:
                x, y, w, h = f[:4]
                lm = f[4:14].reshape(5, 2)  # r-eye, l-eye, nose, r-mouth, l-mouth
                out.append({'x': float(x), 'y': float(y), 'w': float(w), 'h': float(h),
                            'score': float(f[14]), 'mouth': lm[3:5].tolist(), 'eyes': lm[0:2].tolist()})
        else:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            for (x, y, w, h) in self.det.detectMultiScale(g, 1.1, 5, minSize=(40, 40)):
                out.append({'x': float(x), 'y': float(y), 'w': float(w), 'h': float(h), 'score': 0.8,
                            'mouth': [[x + 0.3 * w, y + 0.75 * h], [x + 0.7 * w, y + 0.75 * h]],
                            'eyes': [[x + 0.3 * w, y + 0.38 * h], [x + 0.7 * w, y + 0.38 * h]]})
        return [f for f in out if f['w'] >= 28]


def torso_descriptor(hsv, f):
    """Clothing colour signature (HSV histogram of the chest below the face).

    Used to recognise the same person across camera cuts (shirts don't change
    between the wide and close shots of a podcast)."""
    h, w = hsv.shape[:2]
    x0 = int(max(0, f['cx'] - 0.8 * f['w']))
    x1 = int(min(w, f['cx'] + 0.8 * f['w']))
    y0 = int(min(h - 1, f['y'] + 1.4 * f['h']))
    y1 = int(min(h, f['y'] + 2.6 * f['h']))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    roi = hsv[y0:y1, x0:x1]
    hist = cv2.calcHist([roi], [0, 1, 2], None, [8, 3, 4], [0, 180, 0, 256, 0, 256]).flatten()
    hist /= hist.sum() + 1e-9
    return [round(float(v), 4) for v in hist]


def _roi(gray, cx, cy, rw, rh):
    h, w = gray.shape
    x0, x1 = int(max(0, cx - rw / 2)), int(min(w, cx + rw / 2))
    y0, y1 = int(max(0, cy - rh / 2)), int(min(h, cy + rh / 2))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None, None
    return (x0, y0, x1, y1), gray[y0:y1, x0:x1]


def analyze(path, out_json=None):
    info = probe(path)
    W, H = info['w'], info['h']
    aw = ANALYSIS_W
    ah = int(round(H * aw / W / 2) * 2)
    scale = W / aw
    shots_cuts = detect_shots(path)
    det = Detector(aw, ah)
    samples, tracks_next = [], 0
    prev_gray, prev_shot = None, -1
    tracks = []  # per-shot track table: {id, cx, cy, w, last_t}

    half_frame = 0.5 / info['fps']

    def shot_index(t):
        # half-frame tolerance: cut times are rounded, a sample on the cut belongs to the new shot
        return sum(1 for c in shots_cuts if c <= t + half_frame)

    for t, img in iter_frames(path, SAMPLE_FPS, aw, ah):
        gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        faces = det(img)
        shot = shot_index(t)
        same_shot = shot == prev_shot
        if not same_shot:
            tracks = []
        for f in faces:
            f['cx'] = f['x'] + f['w'] / 2
            f['cy'] = f['y'] + f['h'] / 2
        # greedy nearest-centre association against tracks seen in this shot (tolerates missed detections)
        pairs = []
        for fi, f in enumerate(faces):
            for ti, tr in enumerate(tracks):
                d = np.hypot(tr['cx'] - f['cx'], tr['cy'] - f['cy'])
                if d < 0.9 * max(f['w'], tr['w']) and t - tr['last_t'] < 3.0:
                    pairs.append((d, fi, ti))
        used_f, used_t = set(), set()
        for d, fi, ti in sorted(pairs):
            if fi in used_f or ti in used_t:
                continue
            used_f.add(fi)
            used_t.add(ti)
            faces[fi]['id'] = tracks[ti]['id']
            tracks[ti].update(cx=faces[fi]['cx'], cy=faces[fi]['cy'], w=faces[fi]['w'], last_t=t)
        for fi, f in enumerate(faces):
            if fi not in used_f:
                f['id'] = tracks_next
                tracks.append({'id': tracks_next, 'cx': f['cx'], 'cy': f['cy'], 'w': f['w'], 'last_t': t})
                tracks_next += 1
        for f in faces:
            # mouth activity vs the previous sample at the same location
            act = 0.0
            if prev_gray is not None and same_shot:
                (mx1, my1), (mx2, my2) = f['mouth']
                mcx, mcy = (mx1 + mx2) / 2, (my1 + my2) / 2 + 0.06 * f['h']
                mw = max(abs(mx2 - mx1) * 1.6, 0.35 * f['w'])
                mh = 0.32 * f['h']
                box, cur = _roi(gray, mcx, mcy, mw, mh)
                (ex1, ey1), (ex2, ey2) = f['eyes']
                ebox, ecur = _roi(gray, (ex1 + ex2) / 2, (ey1 + ey2) / 2, 0.7 * f['w'], 0.22 * f['h'])
                if box is not None and ebox is not None:
                    x0, y0, x1, y1 = box
                    md = float(np.mean(cv2.absdiff(cur, prev_gray[y0:y1, x0:x1])))
                    x0, y0, x1, y1 = ebox
                    ed = float(np.mean(cv2.absdiff(ecur, prev_gray[y0:y1, x0:x1])))
                    act = max(0.0, md - 0.8 * ed)
            f['act'] = act
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        for f in faces:
            f['desc'] = torso_descriptor(hsv, f)
        samples.append({'t': round(t, 3), 'faces': [
            {'id': f['id'], 'cx': f['cx'] * scale, 'cy': f['cy'] * scale, 'w': f['w'] * scale,
             'h': f['h'] * scale, 'score': f['score'], 'act': round(f['act'], 3),
             'desc': f['desc']} for f in faces]})
        prev_gray, prev_shot = gray, shot

    shots, t0 = [], 0.0
    for c in shots_cuts + [info['duration']]:
        if c - t0 > 0.04:
            shots.append([round(t0, 3), round(c, 3)])
        t0 = c
    res = {'version': VERSION, 'video': info, 'detector': det.kind, 'sample_fps': SAMPLE_FPS, 'shots': shots, 'samples': samples}
    if out_json:
        Path(out_json).write_text(json.dumps(res), encoding='utf-8')
    return res


if __name__ == '__main__':
    import sys
    r = analyze(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    n = sum(len(s['faces']) for s in r['samples'])
    print(f"detector={r['detector']} shots={len(r['shots'])} samples={len(r['samples'])} faces={n}")
