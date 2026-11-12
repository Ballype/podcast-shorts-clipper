# Cloud side of the daily run, executed in COMPOSIO_REMOTE_WORKBENCH (session-persistent sandbox).
# Run the three CELLS as three separate workbench calls (each call has a 180 s limit).
# Tested end-to-end on 2026-11-08 (3 private YouTube drafts + Telegram notifications).
#
# Known limits (verified):
# - Composio's Telegram proxy does not add the bot token, so videos can't be pushed into the
#   chat; the user gets a download link (GitHub staging URL) instead.
# - YOUTUBE_MULTIPART_UPLOAD_VIDEO returns the id at data.video.id.

# ======================= CELL 1: payload + staging -> Composio storage =======================
import json, os, requests, time
from concurrent.futures import ThreadPoolExecutor
PAYLOAD = json.loads(r'''__PAYLOAD_JSON__''')   # built in RUNBOOK step 8
json.dump(PAYLOAD, open('/mnt/files/payload.json', 'w'))
os.makedirs('/home/user/clips', exist_ok=True)

def fetch(c):
    r = requests.get(c['staging_url'], timeout=160); r.raise_for_status()
    p = f"/home/user/clips/{c['file']}"; open(p, 'wb').write(r.content)
    up, err = upload_local_file(p)
    return {'id': c['id'], 'err': err, 's3key': (up.get('data') or up).get('s3key') if not err else None}

with ThreadPoolExecutor(4) as ex:
    staged = list(ex.map(fetch, PAYLOAD['clips']))
json.dump(staged, open('/mnt/files/staged.json', 'w'))
print([(s['id'], 'ok' if s['s3key'] else s['err']) for s in staged])

# ======================= CELL 2: YouTube private drafts =======================
import json
from concurrent.futures import ThreadPoolExecutor
P = json.load(open('/mnt/files/payload.json'))
S = {s['id']: s for s in json.load(open('/mnt/files/staged.json'))}

def yt(c):
    s = S[c['id']]
    if not s.get('s3key'):
        return {'id': c['id'], 'err': 'not staged'}
    res, err = run_composio_tool('YOUTUBE_MULTIPART_UPLOAD_VIDEO', {
        'title': c['yt_title'][:100], 'description': c['yt_description'][:4900], 'tags': c['tags'],
        'categoryId': P['category_id'], 'privacyStatus': P['privacy'],
        'videoFile': {'name': c['file'], 'mimetype': 'video/mp4', 's3key': s['s3key']}})
    vid = ((res or {}).get('data') or {}).get('video', {}).get('id') if not err else None
    return {'id': c['id'], 'err': err, 'youtube_id': vid}

with ThreadPoolExecutor(4) as ex:
    ups = list(ex.map(yt, P['clips']))
json.dump(ups, open('/mnt/files/youtube.json', 'w'))
print([(u['id'], u.get('youtube_id') or u['err']) for u in ups])

# ======================= CELL 3: Telegram "batch ready" summary =======================
# Per-clip "post now" messages (with the video file) are sent later by scripts/daily/remind.py
# at the posting times, so this only confirms the batch.
import json
P = json.load(open('/mnt/files/payload.json'))
Y = {u['id']: u for u in json.load(open('/mnt/files/youtube.json'))}
ok = [c for c in P['clips'] if Y.get(c['id'], {}).get('youtube_id')]
lines = '\n'.join(f"• {c['yt_title']} ({c['duration']}s, score {c['score']})" for c in P['clips'])
text = (f"🎬 Shorts: new batch ready, {len(ok)}/{len(P['clips'])} clips uploaded as private YouTube drafts\n"
        f"From: {P['source_title']}\n\n{lines}\n\n"
        "They're queued; the cloud poster publishes them to Instagram + YouTube at 09:00, 14:00 and 19:00, "
        "even if the PC is off. You'll get a confirmation here after each post.")
print(run_composio_tool('TELEGRAM_SEND_MESSAGE', {'chat_id': P['telegram_chat_id'], 'text': text[:4000],
                                                  'disable_web_page_preview': True})[1] or 'ok')
