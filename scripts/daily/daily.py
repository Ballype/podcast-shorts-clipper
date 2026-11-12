"""Helpers for the daily Shorts run (see daily/RUNBOOK.md).

    python scripts/daily/daily.py pick                 -> JSON of the next video to clip
    python scripts/daily/daily.py stage <dir> <tag>    -> uploads clips to the public staging repo, prints asset URLs
    python scripts/daily/daily.py cleanup              -> deletes staging releases older than staging_keep_days
    python scripts/daily/daily.py mark <video_id> <n>  -> records a video as clipped (n clips)
    python scripts/daily/daily.py status               -> processed videos so far
    python scripts/daily/daily.py release <dir> <batch> -> uploads clips to release clips-<batch> on the main repo (cloud poster source)
    python scripts/daily/daily.py enqueue <payload> <ids> <clips_dir> <batch> -> adds the batch to daily/queue.json
    python scripts/daily/daily.py next                -> next `ready` clip to publish (POSTING.md)
    python scripts/daily/daily.py posted <uid> [ig_media_id] [ig_permalink] -> mark a clip as published
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CFG = json.loads((REPO / 'daily' / 'config.json').read_text(encoding='utf-8'))
STATE = REPO / 'daily' / 'state.json'


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding='utf-8'))
    return {'processed': {}}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding='utf-8')


def ytdlp():
    extra = [str(Path(sys.executable).parent / 'Scripts'),
             os.path.expandvars(r'%APPDATA%\Python\Python314\Scripts'),
             os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\WinGet\Links'),
             os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\WinGet\Packages\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe')]
    env = dict(os.environ)
    env['PATH'] = env.get('PATH', '') + os.pathsep + os.pathsep.join(extra)
    env['PYTHONUTF8'] = '1'
    return env


def pick():
    """Newest unprocessed long-form video across the configured channels (falls back to the back catalogue)."""
    state = load_state()
    candidates = []
    for ch in CFG['channels']:
        r = subprocess.run(['yt-dlp', '--flat-playlist', '-I', f"1:{CFG['scan_latest']}", '--print',
                            '%(id)s\t%(duration)s\t%(title)s', ch['url']],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', env=ytdlp(),
                           stdin=subprocess.DEVNULL)
        for i, line in enumerate(r.stdout.splitlines()):
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            vid, dur, title = parts
            try:
                dur = float(dur)
            except ValueError:
                continue
            if dur < CFG['min_video_minutes'] * 60 or vid in state['processed']:
                continue
            candidates.append({'id': vid, 'duration_min': round(dur / 60, 1), 'title': title, 'channel': ch['name'],
                               'url': f'https://www.youtube.com/watch?v={vid}', 'rank': i})
    if not candidates:
        print(json.dumps({'error': 'no unprocessed videos found; add channels to daily/config.json or raise scan_latest'}))
        return
    candidates.sort(key=lambda c: c['rank'])  # newest first, then older episodes
    print(json.dumps(candidates[0], ensure_ascii=False))


def stage(clip_dir, tag):
    files = sorted(str(p) for p in Path(clip_dir).glob('*.mp4'))
    if not files:
        sys.exit(f'no mp4 files in {clip_dir}')
    repo = CFG['staging_repo']
    subprocess.run(['gh', 'release', 'delete', tag, '--repo', repo, '--yes', '--cleanup-tag'],
                   capture_output=True, stdin=subprocess.DEVNULL)
    r = subprocess.run(['gh', 'release', 'create', tag, *files, '--repo', repo, '--title', tag,
                        '--notes', 'Temporary staging for Shorts clips. Deleted automatically.'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        sys.exit(f'gh release create failed: {r.stderr}')
    out = {Path(f).name: f'https://github.com/{repo}/releases/download/{tag}/{Path(f).name}' for f in files}
    print(json.dumps(out, indent=2))


def cleanup():
    repo = CFG['staging_repo']
    r = subprocess.run(['gh', 'release', 'list', '--repo', repo, '--json', 'tagName,createdAt', '--limit', '100'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    removed = []
    now = datetime.now(timezone.utc)
    for rel in json.loads(r.stdout or '[]'):
        created = datetime.fromisoformat(rel['createdAt'].replace('Z', '+00:00'))
        if (now - created).days >= CFG['staging_keep_days']:
            subprocess.run(['gh', 'release', 'delete', rel['tagName'], '--repo', repo, '--yes', '--cleanup-tag'],
                           capture_output=True, stdin=subprocess.DEVNULL)
            removed.append(rel['tagName'])
    print(json.dumps({'removed': removed}))


QUEUE = REPO / 'daily' / 'queue.json'


def load_queue():
    return json.loads(QUEUE.read_text(encoding='utf-8')) if QUEUE.exists() else []


def save_queue(q):
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding='utf-8')


def buffer():
    """How many clips are ready to announce, in days of posting. need_batch = make a new batch today."""
    q = load_queue()
    ready = [c for c in q if c['status'] == 'ready']
    per_day = CFG['posting']['posts_per_day']
    days = len(ready) / per_day
    print(json.dumps({'ready': len(ready), 'days': round(days, 1), 'posts_per_day': per_day,
                      'buffer_days': CFG['posting']['buffer_days'],
                      'need_batch': days < CFG['posting']['buffer_days']}))


def release(clip_dir, batch):
    """Upload a batch's clips to a release on the (private) main repo; the cloud poster downloads them from there."""
    files = sorted(str(p) for p in Path(clip_dir).glob('*.mp4'))
    if not files:
        sys.exit(f'no mp4 files in {clip_dir}')
    repo, tag = CFG['clips_repo'], f'clips-{batch}'
    subprocess.run(['gh', 'release', 'delete', tag, '--repo', repo, '--yes', '--cleanup-tag'],
                   capture_output=True, stdin=subprocess.DEVNULL)
    r = subprocess.run(['gh', 'release', 'create', tag, *files, '--repo', repo, '--title', tag,
                        '--notes', 'Shorts clips waiting to be posted by the cloud poster. Deleted automatically once all are posted.'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        sys.exit(f'gh release create failed: {r.stderr}')
    print(json.dumps({'release_tag': tag, 'assets': [Path(f).name for f in files]}))


def enqueue(payload_path, ids_path, clips_dir, batch=None):
    """Add a rendered batch to the posting queue (payload = RUNBOOK step 10 JSON, ids = {clip_id: youtube_id})."""
    payload = json.loads(Path(payload_path).read_text(encoding='utf-8'))
    ids = json.loads(Path(ids_path).read_text(encoding='utf-8'))
    q = load_queue()
    batch = batch or time.strftime('%Y-%m-%d') + '-' + payload.get('source_id', 'batch')
    have = {c['uid'] for c in q}
    added = 0
    for c in sorted(payload['clips'], key=lambda c: -c.get('score', 0)):
        uid = f"{batch}-{c['id']}"
        if uid in have:
            continue
        q.append({'uid': uid, 'batch': batch, 'status': 'ready', 'created': time.strftime('%Y-%m-%d %H:%M'),
                  'video_path': str((Path(clips_dir) / c['file']).resolve()), 'youtube_id': ids.get(c['id']),
                  'release_tag': f'clips-{batch}', 'asset': c['file'],
                  'yt_title': c['yt_title'], 'yt_description': c.get('yt_description', ''), 'tags': c.get('tags', []),
                  'ig_caption': c['ig_caption'], 'pinned_comment': c['pinned_comment'],
                  'duration': c.get('duration'), 'score': c.get('score'), 'source_title': payload.get('source_title', '')})
        added += 1
    save_queue(q)
    print(json.dumps({'added': added, 'ready_total': sum(1 for c in q if c['status'] == 'ready')}))


def unstage(tag):
    r = subprocess.run(['gh', 'release', 'delete', tag, '--repo', CFG['staging_repo'], '--yes', '--cleanup-tag'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    print(json.dumps({'unstaged': tag, 'ok': r.returncode == 0}))


def next_clip():
    """The clip to publish at this posting slot (first `ready` in the queue), or {"none": true}."""
    ready = [c for c in load_queue() if c['status'] == 'ready']
    if not ready:
        print(json.dumps({'none': True}))
        return
    c = dict(ready[0])
    c['file_exists'] = Path(c['video_path']).exists()
    c['ready_left_after'] = len(ready) - 1
    print(json.dumps(c, indent=2, ensure_ascii=False))


def posted(uid, ig_media_id='', ig_permalink=''):
    """Mark a queued clip as published by hand (the cloud poster does this itself)."""
    q = load_queue()
    for c in q:
        if c['uid'] == uid:
            c['status'] = 'posted'
            c['posted_at'] = time.strftime('%Y-%m-%d %H:%M')
            if ig_media_id:
                c['ig_media_id'] = ig_media_id
            if ig_permalink:
                c['ig_permalink'] = ig_permalink
            save_queue(q)
            print(json.dumps({'posted': uid, 'ready_left': sum(1 for x in q if x['status'] == 'ready')}))
            return
    sys.exit(f'uid not in queue: {uid}')


def mark(video_id, n):
    s = load_state()
    s['processed'][video_id] = {'date': time.strftime('%Y-%m-%d'), 'clips': int(n)}
    save_state(s)
    print(json.dumps({'marked': video_id, 'total_processed': len(s['processed'])}))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'pick':
        pick()
    elif cmd == 'stage':
        stage(sys.argv[2], sys.argv[3])
    elif cmd == 'cleanup':
        cleanup()
    elif cmd == 'mark':
        mark(sys.argv[2], sys.argv[3])
    elif cmd == 'buffer':
        buffer()
    elif cmd == 'enqueue':
        enqueue(*sys.argv[2:6])
    elif cmd == 'release':
        release(sys.argv[2], sys.argv[3])
    elif cmd == 'unstage':
        unstage(sys.argv[2])
    elif cmd == 'next':
        next_clip()
    elif cmd == 'posted':
        posted(*sys.argv[2:5])
    else:
        print(json.dumps(load_state(), indent=2))
