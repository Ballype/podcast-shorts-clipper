"""Posting reminder: sends the next queued clip to Telegram. No Claude, no Composio.

Run by Windows Task Scheduler at the posting times (daily/config.json -> posting.times),
so reminders keep working even when Claude credits run out.

Needs daily/.env containing:   TELEGRAM_BOT_TOKEN=123456:ABC...
(daily/.env is gitignored; it never leaves this PC.)

    python scripts/daily/remind.py          -> send the next clip
    python scripts/daily/remind.py --test   -> send a test message only
"""
import json
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]
CFG = json.loads((REPO / 'daily' / 'config.json').read_text(encoding='utf-8'))
QUEUE = REPO / 'daily' / 'queue.json'
LOG = REPO / 'daily' / 'remind.log'


def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def token():
    env = REPO / 'daily' / '.env'
    if not env.exists():
        sys.exit('daily/.env missing (TELEGRAM_BOT_TOKEN=...)')
    for line in env.read_text(encoding='utf-8').splitlines():
        if line.strip().startswith('TELEGRAM_BOT_TOKEN='):
            return line.split('=', 1)[1].strip()
    sys.exit('TELEGRAM_BOT_TOKEN not found in daily/.env')


API = f'https://api.telegram.org/bot{token()}'
CHAT = CFG['telegram_chat_id']


def send_text(text):
    r = requests.post(f'{API}/sendMessage', data={'chat_id': CHAT, 'text': text[:4000],
                                                  'disable_web_page_preview': 'true'}, timeout=60)
    r.raise_for_status()


def send_video(path, caption):
    with open(path, 'rb') as f:
        r = requests.post(f'{API}/sendVideo', data={'chat_id': CHAT, 'caption': caption[:1000],
                                                    'supports_streaming': 'true'},
                          files={'video': (Path(path).name, f, 'video/mp4')}, timeout=600)
    if not r.ok:
        raise RuntimeError(f'sendVideo {r.status_code}: {r.text[:300]}')


def main():
    if '--test' in sys.argv:
        send_text('✅ Shorts reminders are connected. Clips will arrive here at posting time.')
        print('test message sent')
        return
    q = json.loads(QUEUE.read_text(encoding='utf-8')) if QUEUE.exists() else []
    ready = [c for c in q if c['status'] == 'ready']
    if not ready:
        send_text('⚠️ Shorts: no clips left in the queue. The daily 06:00 run should make more. '
                  'If it keeps failing (e.g. out of Claude credit), open the Claude app and run "Shorts daily clips".')
        log('queue empty')
        return
    c = ready[0]
    left = len(ready) - 1
    yt = f"https://studio.youtube.com/video/{c['youtube_id']}/edit" if c.get('youtube_id') else 'not uploaded; upload this file manually'
    if Path(c['video_path']).exists():
        send_video(c['video_path'], f"⏰ Time to post: {c['yt_title']}")
    else:
        send_text(f"⚠️ Video file missing on the PC: {c['video_path']}")
    send_text(
        f"⏰ POST NOW — {c['yt_title']}  ({c.get('duration')}s, score {c.get('score')})\n\n"
        f"1) YouTube: open the draft → Visibility → Public\n{yt}\n\n"
        f"2) Instagram: save the video above → new Reel → paste:\n\n{c['ig_caption']}\n\n"
        f"3) Pin this comment on both:\n{c['pinned_comment']}\n\n"
        f"Queue: {left} clip(s) left (~{left / CFG['posting']['posts_per_day']:.1f} days)."
    )
    c['status'] = 'announced'
    c['announced_at'] = time.strftime('%Y-%m-%d %H:%M')
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding='utf-8')
    log(f"announced {c['uid']} ({left} left)")
    print(f"sent {c['uid']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # keep a trace for debugging scheduled runs
        log(f'ERROR {e}')
        raise
