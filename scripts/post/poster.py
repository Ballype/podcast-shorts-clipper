"""Cloud poster for Shorts: runs in GitHub Actions (.github/workflows/post.yml), no PC or Claude needed.

Every run checks whether a posting slot (daily/config.json -> posting.times, local time) has passed
without a post. If so it publishes the next `ready` clip from daily/queue.json:
  Instagram Reel (public) + flips the clip's private YouTube draft to public + comments + Telegram note.
Missed slots are caught up on the next run (up to posting.max_late_hours late); if several slots were
missed, only the most recent is posted so clips never go out back to back.

    python scripts/post/poster.py --due       -> prints the due slot (or nothing), exit 0; no deps, no network
    python scripts/post/poster.py --check     -> verifies Composio + Telegram credentials (read-only)
    python scripts/post/poster.py --dry-run   -> shows what would be posted, publishes nothing
    python scripts/post/poster.py             -> post if a slot is due

Env: COMPOSIO_API_KEY, TELEGRAM_BOT_TOKEN, GH_TOKEN (Actions provides it), optional COMPOSIO_USER_ID.
The caller commits daily/queue.json + daily/post_log.json afterwards.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CFG = json.loads((REPO / 'daily' / 'config.json').read_text(encoding='utf-8'))
QUEUE = REPO / 'daily' / 'queue.json'
LOG = REPO / 'daily' / 'post_log.json'
POST = CFG['posting']
TZ = timezone(timedelta(hours=POST.get('utc_offset_hours', 1)))
GH_REPO = CFG['clips_repo']


def load(p, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def save(p, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def due_slot(now=None):
    """Most recent slot that has passed, is within max_late_hours, and has no log entry. Earlier missed ones are returned as skipped."""
    now = now or datetime.now(TZ)
    log = load(LOG, {'slots': {}})
    late = timedelta(hours=POST.get('max_late_hours', 6))
    passed = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for t in POST['times']:
            h, m = map(int, t.split(':'))
            slot = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
            if slot <= now and now - slot <= late:
                passed.append(slot.strftime('%Y-%m-%d %H:%M'))
    open_slots = [s for s in passed if s not in log['slots']]
    if not open_slots:
        return None, []
    return open_slots[-1], open_slots[:-1]


# ---------------------------------------------------------------- integrations
def telegram(text, video=None):
    import requests
    tok = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not tok:
        print('(no TELEGRAM_BOT_TOKEN; message not sent)\n' + text)
        return
    api = f'https://api.telegram.org/bot{tok}'
    chat = CFG['telegram_chat_id']
    if video:
        with open(video, 'rb') as f:
            requests.post(f'{api}/sendVideo', data={'chat_id': chat, 'caption': text[:1000], 'supports_streaming': 'true'},
                          files={'video': (Path(video).name, f, 'video/mp4')}, timeout=600)
        return
    r = requests.post(f'{api}/sendMessage', data={'chat_id': chat, 'text': text[:4000], 'disable_web_page_preview': 'true'},
                      timeout=60)
    if not r.ok:
        raise SystemExit(f'Telegram sendMessage failed {r.status_code}: {r.text[:300]}')


def youtube_configured():
    return all(os.environ.get(k) for k in ('YT_CLIENT_ID', 'YT_CLIENT_SECRET', 'YT_REFRESH_TOKEN'))


class Composio:
    def __init__(self):
        from composio import Composio as C
        self.c = C(api_key=os.environ['COMPOSIO_API_KEY'])
        self.accounts = {}
        everything = self.c.connected_accounts.list(account_type='ALL', limit=100).items
        uid = os.environ.get('COMPOSIO_USER_ID')

        def tk_of(a):
            t = getattr(a, 'toolkit', None)
            return (getattr(t, 'slug', None) or (t.get('slug') if isinstance(t, dict) else None) or '').lower()

        needed = ('instagram',) if youtube_configured() else ('instagram', 'youtube')
        for tk in needed:
            items = [a for a in everything if tk_of(a) == tk and str(getattr(a, 'status', '')).upper().endswith('ACTIVE')]
            items = [a for a in items if not uid or getattr(a, 'user_id', None) == uid] or items
            if not items:
                seen = [(tk_of(a), getattr(a, 'status', None), getattr(a, 'user_id', None), a.id) for a in everything]
                raise SystemExit(f'no ACTIVE Composio connection for {tk}. Accounts visible to this API key: {seen}')
            # Prefer connections on our own OAuth app (own API quota) over Composio's shared one, newest first.
            items.sort(key=lambda a: (self.managed(a), str(getattr(a, 'created_at', ''))), reverse=False)
            items.sort(key=lambda a: str(getattr(a, 'created_at', '')), reverse=True)
            items.sort(key=lambda a: self.managed(a))
            self.accounts[tk] = items[0]
            self.all = {**getattr(self, 'all', {}), tk: items}

    def upload(self, path, tool, toolkit):
        """Upload a local file to Composio storage for a file_uploadable tool argument."""
        from composio.core.models._files import FileUploadable
        f = FileUploadable.from_path(self.c._client, path, tool, toolkit, file_upload_allowlist=None)
        return f.model_dump()

    @staticmethod
    def managed(a):
        ac = getattr(a, 'auth_config', None)
        return bool(getattr(ac, 'is_composio_managed', True))

    def run(self, slug, args):
        tk = slug.split('_', 1)[0].lower()
        acc = self.accounts[tk]
        r = self.c.tools.execute(slug, args, connected_account_id=acc.id, user_id=getattr(acc, 'user_id', None),
                                 dangerously_skip_version_check=True)
        r = r if isinstance(r, dict) else r.model_dump()
        if not r.get('successful', True):
            return None, r.get('error') or 'failed'
        return r.get('data') or {}, None


def download(clip, dest):
    subprocess.run(['gh', 'release', 'download', clip['release_tag'], '--repo', GH_REPO, '--pattern', clip['asset'],
                    '--dir', dest, '--clobber'], check=True)
    return str(Path(dest) / clip['asset'])


def youtube_direct():
    """YouTube Data API client from our own OAuth app (secrets YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN,
    created once with scripts/post/youtube_auth.py). None if not configured -> fall back to Composio."""
    if not all(os.environ.get(k) for k in ('YT_CLIENT_ID', 'YT_CLIENT_SECRET', 'YT_REFRESH_TOKEN')):
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=os.environ['YT_REFRESH_TOKEN'], token_uri='https://oauth2.googleapis.com/token',
                        client_id=os.environ['YT_CLIENT_ID'], client_secret=os.environ['YT_CLIENT_SECRET'])
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def publish_youtube_direct(clip, path, yt):
    """Upload (or flip an existing draft) to public and post the comment. Returns (out dict)."""
    from googleapiclient.http import MediaFileUpload
    out = {}
    try:
        yid = clip.get('youtube_id')
        if yid:
            yt.videos().update(part='status', body={'id': yid, 'status': {'privacyStatus': 'public'}}).execute()
        else:
            body = {'snippet': {'title': clip['yt_title'][:100],
                                'description': (clip.get('yt_description') or clip['yt_title'])[:4900],
                                'tags': clip.get('tags') or CFG['youtube']['default_tags'],
                                'categoryId': CFG['youtube']['category_id']},
                    'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}}
            req = yt.videos().insert(part='snippet,status', body=body,
                                     media_body=MediaFileUpload(path, mimetype='video/mp4', resumable=True, chunksize=8 * 1024 * 1024))
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            yid = resp['id']
            clip['youtube_id'] = yid
        v = yt.videos().list(part='snippet,status', id=yid).execute()['items'][0]
        out['yt_public'] = v['status']['privacyStatus'] == 'public'
        try:
            yt.commentThreads().insert(part='snippet', body={'snippet': {
                'channelId': v['snippet']['channelId'], 'videoId': yid,
                'topLevelComment': {'snippet': {'textOriginal': clip['pinned_comment']}}}}).execute()
        except Exception as e:  # comment failing must not fail the post
            out['yt_comment_err'] = str(e)[:300]
    except Exception as e:
        out['yt_err'] = ' '.join(str(e).split())[:400]
    return out


# ---------------------------------------------------------------- posting
def publish(clip, path, cz, yt=None):
    out = {}
    ig_user = CFG.get('instagram_user_id', 'me')
    # The SDK does not auto-upload local paths (that needs an opt-in flag), so upload the video to
    # Composio storage explicitly and pass the resulting {name, mimetype, s3key} object.
    try:
        video = cz.upload(path, 'INSTAGRAM_POST_IG_USER_MEDIA', 'instagram')
        data, err = cz.run('INSTAGRAM_POST_IG_USER_MEDIA', {'ig_user_id': ig_user, 'media_type': 'REELS', 'share_to_feed': True,
                                                            'caption': clip['ig_caption'], 'video_file': video})
    except Exception as e:
        data, err = None, f'upload failed: {e}'
    cid = (data or {}).get('id')
    if cid:
        data, err = cz.run('INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH', {'ig_user_id': ig_user, 'creation_id': cid,
                                                                    'max_wait_seconds': 240, 'poll_interval_seconds': 5})
    media = (data or {}).get('id') if cid else None
    out['ig_media_id'], out['ig_err'] = media, None if media else str(err)[:300]
    if media:
        meta, _ = cz.run('INSTAGRAM_GET_IG_MEDIA', {'ig_media_id': media, 'fields': 'id,permalink,caption'})
        out['ig_permalink'] = (meta or {}).get('permalink')
        out['caption_ok'] = ((meta or {}).get('caption') or '')[:40].strip() == clip['ig_caption'][:40].strip()
        _, out['ig_comment_err'] = cz.run('INSTAGRAM_POST_IG_MEDIA_COMMENTS', {'ig_media_id': media, 'message': clip['pinned_comment'][:300]})

    if yt is not None:
        out.update(publish_youtube_direct(clip, path, yt))
        return out
    yid = clip.get('youtube_id')
    if not yid:
        # No draft was uploaded ahead (e.g. upload quota was hit during the batch): upload it now, straight to public.
        data, out['yt_err'] = cz.run('YOUTUBE_MULTIPART_UPLOAD_VIDEO', {
            'title': clip['yt_title'][:100], 'description': (clip.get('yt_description') or clip['yt_title'])[:4900],
            'tags': clip.get('tags') or CFG['youtube']['default_tags'], 'categoryId': CFG['youtube']['category_id'],
            'privacyStatus': 'public', 'videoFile': path})
        yid = ((data or {}).get('video') or {}).get('id')
        if yid:
            clip['youtube_id'] = yid
    if yid:
        if not out.get('yt_err'):
            _, out['yt_err'] = cz.run('YOUTUBE_UPDATE_VIDEO', {'video_id': yid, 'privacy_status': 'public'})
        det, _ = cz.run('YOUTUBE_GET_VIDEO_DETAILS_BATCH', {'id': [yid], 'parts': ['snippet', 'status']})
        items = (det or {}).get('items') or []
        if items:
            out['yt_public'] = items[0].get('status', {}).get('privacyStatus') == 'public'
            ch = items[0].get('snippet', {}).get('channelId')
            if ch and out['yt_public']:
                _, out['yt_comment_err'] = cz.run('YOUTUBE_POST_COMMENT', {'videoId': yid, 'channelId': ch,
                                                                           'textOriginal': clip['pinned_comment']})
    return out


def cleanup_releases(q):
    """Delete a batch's clips release once none of its clips are still waiting."""
    batches = {c['release_tag'] for c in q if c.get('release_tag')}
    for tag in batches:
        if all(c['status'] != 'ready' for c in q if c.get('release_tag') == tag):
            subprocess.run(['gh', 'release', 'delete', tag, '--repo', GH_REPO, '--yes', '--cleanup-tag'], capture_output=True)
            for c in q:
                if c.get('release_tag') == tag:
                    c['release_deleted'] = True


def main():
    slot, skipped = due_slot()
    if '--due' in sys.argv:
        print(slot or '')
        return
    if '--telegram' in sys.argv:
        try:
            from composio import Composio as C
            accts = C(api_key=os.environ['COMPOSIO_API_KEY']).connected_accounts.list(account_type='ALL', limit=100).items
            print('Composio accounts visible to this key:',
                  [(getattr(getattr(a, 'toolkit', None), 'slug', None), getattr(a, 'status', None)) for a in accts])
        except Exception as e:  # diagnostics only
            print('Composio listing failed:', e)
        telegram('✅ Shorts cloud poster: Telegram works. Post confirmations and alerts will arrive here.')
        print('telegram test message sent')
        return
    if '--igtest' in sys.argv:
        # Upload the next queued clip and create an Instagram container WITHOUT publishing it
        # (unpublished containers expire by themselves). Proves the upload path works.
        q = load(QUEUE, [])
        clip = next(c for c in q if c['status'] == 'ready' and c.get('release_tag'))
        cz = Composio()
        with tempfile.TemporaryDirectory() as tmp:
            path = download(clip, tmp)
            video = cz.upload(path, 'INSTAGRAM_POST_IG_USER_MEDIA', 'instagram')
            print('uploaded to Composio storage:', {k: video[k] for k in ('name', 'mimetype')})
            data, err = cz.run('INSTAGRAM_POST_IG_USER_MEDIA', {'ig_user_id': CFG.get('instagram_user_id', 'me'),
                                                                'media_type': 'REELS', 'caption': 'test container (not published)',
                                                                'video_file': video})
            print('instagram container:', (data or {}).get('id'), err or 'OK (not published)')
        return
    if '--check' in sys.argv:
        cz = Composio()
        for tk, items in cz.all.items():
            for a in items:
                print(tk, 'ACTIVE account', a.id, 'created', getattr(a, 'created_at', '?'),
                      'auth_config', getattr(getattr(a, 'auth_config', None), 'id', '?'),
                      'composio_managed' if cz.managed(a) else 'OWN_APP', '<- used' if a is cz.accounts[tk] else '')
        info, err = cz.run('INSTAGRAM_GET_USER_INFO', {'ig_user_id': 'me'})
        print('instagram:', (info or {}).get('username'), err or '')
        yt = youtube_direct()
        if yt is not None:
            try:
                ch = yt.channels().list(part='snippet', mine=True).execute().get('items', [])
                print('youtube (direct, own Google app): channel =', ch[0]['snippet']['title'] if ch else 'NONE')
            except Exception as e:
                print('youtube (direct) FAILED:', ' '.join(str(e).split())[:600])
            telegram('✅ Shorts cloud poster: credentials check done (see GitHub log).')
            return
        chans, err = cz.run('YOUTUBE_LIST_CHANNEL_VIDEOS', {'mine': True, 'maxResults': 1})
        print('youtube list mine:', 'ok' if not err else err)
        # A private video owned by YourChannel is only visible to the channel that owns it.
        det, derr = cz.run('YOUTUBE_GET_VIDEO_DETAILS_BATCH', {'id': [CFG['youtube'].get('test_video_id') or 'none'], 'parts': ['snippet', 'status']})
        items = (det or {}).get('items') or []
        if items:
            print('youtube owns YourChannel video:', items[0]['snippet'].get('channelTitle'), items[0]['status'].get('privacyStatus'))
        else:
            print('youtube: cannot see the private YourChannel test video ->', ' '.join(str(derr or 'not visible (wrong channel?)').split())[:1500])
        telegram('✅ Shorts cloud poster: credentials check passed.')
        return
    if not slot:
        print('no slot due')
        return

    q = load(QUEUE, [])
    log = load(LOG, {'slots': {}})
    for s in skipped:
        log['slots'][s] = {'status': 'skipped', 'reason': 'missed; a later slot was due'}
    stuck = [c for c in q if c['status'] == 'posting']
    ready = [c for c in q if c['status'] == 'ready' and c.get('release_tag')]
    if stuck:
        msg = f"⚠️ Shorts: a previous post was interrupted ({stuck[0]['yt_title']}). Check Instagram/YouTube, then set its status in daily/queue.json to 'posted' or 'ready'."
        log['slots'][slot] = {'status': 'blocked', 'uid': stuck[0]['uid']}
    elif not ready:
        msg = '⚠️ Shorts: no clips left to post. Turn on the PC / Claude app so the batch run can make more.'
        log['slots'][slot] = {'status': 'empty'}
    else:
        msg = None
    if msg:
        print(msg)
        if '--dry-run' not in sys.argv:
            telegram(msg)
            save(LOG, log)
        return

    clip = ready[0]
    print(f"slot {slot}: posting {clip['uid']} - {clip['yt_title']}")
    if '--dry-run' in sys.argv:
        print(json.dumps({'would_post': clip['uid'], 'asset': f"{clip['release_tag']}/{clip['asset']}",
                          'youtube_id': clip.get('youtube_id'), 'skipped_slots': skipped}, indent=2, ensure_ascii=False))
        return

    cz = Composio()
    with tempfile.TemporaryDirectory() as tmp:
        path = download(clip, tmp)
        # Mark before publishing so a crash mid-way can never cause a double post.
        clip['status'] = 'posting'
        save(QUEUE, q)
        subprocess.run(['git', 'commit', '-qm', f"poster: posting {clip['uid']}", '--', str(QUEUE)], cwd=REPO)
        subprocess.run(['git', 'pull', '-q', '--rebase'], cwd=REPO)
        subprocess.run(['git', 'push', '-q'], cwd=REPO)
        q = load(QUEUE, q)  # re-read after the rebase in case the PC pushed new clips meanwhile
        clip = next(c for c in q if c['uid'] == clip['uid'])
        res = publish(clip, path, cz, youtube_direct())
        ok_ig, ok_yt = bool(res.get('ig_media_id')), bool(res.get('yt_public'))
        clip.update({k: v for k, v in res.items() if v is not None})
        clip['status'] = 'posted' if (ok_ig or ok_yt) else 'ready'
        clip['slot'] = slot
        clip['posted_at'] = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
        log['slots'][slot] = {'status': clip['status'], 'uid': clip['uid'], 'ig': ok_ig, 'yt': ok_yt}
        left = sum(1 for c in q if c['status'] == 'ready')
        yid = clip.get('youtube_id')
        text = (f"{'✅' if ok_ig and ok_yt else '⚠️'} Shorts posted: {clip['yt_title']}\n\n"
                f"Instagram: {res.get('ig_permalink') or 'FAILED: ' + str(res.get('ig_err'))}\n"
                f"YouTube: {('https://youtube.com/shorts/' + yid) if ok_yt else 'FAILED: ' + str(res.get('yt_err') or 'no draft id')}\n\n"
                f"Pin this comment (already posted):\n{clip['pinned_comment']}\n\n"
                f"Queue: {left} clip(s) left (~{left / POST['posts_per_day']:.1f} days).")
        if res.get('caption_ok') is False:
            text += '\n\n⚠️ Instagram caption looks different from the plan; check it.'
        if not (ok_ig and ok_yt):
            text += '\n\nThe video is attached so you can post the failed side manually.'
        telegram(text)
        if not (ok_ig and ok_yt):
            telegram(f"Manual post: {clip['yt_title']}\n\n{clip['ig_caption']}", video=path)
        if left < POST['posts_per_day'] * 2:
            telegram(f"ℹ️ Only {left} clip(s) left. Open the PC/Claude app soon so the batch run tops up the week.")
    cleanup_releases(q)
    save(QUEUE, q)
    save(LOG, log)


if __name__ == '__main__':
    main()
