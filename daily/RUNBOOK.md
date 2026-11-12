# Daily Shorts run (06:00)

You are running unattended. The system keeps a **posting queue** (`daily/queue.json`, in git) with
**a week** of ready clips (`posting.buffer_days` = 7, 3 posts/day = 21 clips). Posting is done in the cloud by
GitHub Actions (`.github/workflows/post.yml` → `scripts/post/poster.py`), which needs neither this PC
nor Claude: at 09:00, 14:00 and 19:00 WAT it publishes the next `ready` clip as an Instagram Reel, flips its
YouTube draft to public, and catches up any slot it missed. It downloads the videos from the
`clips-<batch>` GitHub release, so a batch only counts once that release and the queue are **pushed**.
Your job: **top up the queue to a full week**. Turn long videos into publication-ready vertical shorts,
upload them to YouTube as **private drafts**, publish them to the clips release, queue them and push.
Repeat steps 2-11 with the next video until `daily.py buffer` says `need_batch: false`, at most
`posting.max_videos_per_run` (3) videos per run.

**Always `git pull --rebase` before touching `daily/queue.json`**: the cloud poster commits to it too.

In this run, **never** publish anything publicly: YouTube privacy is always `private`, and nothing is
posted to Instagram (only the cloud poster publishes). If any step fails and you can't fix it, send a Telegram message
(chat `<YOUR_TELEGRAM_CHAT_ID>`) saying what failed, then stop.

Working directory: `C:\path\to\this\repo` (the repo). Config: `daily/config.json`.
Editing reference: `SHORTS_EDITING.md`. Every shell command needs:
`export PYTHONUTF8=1 PATH="$PATH:$LOCALAPPDATA/Programs/Python/Python314:$LOCALAPPDATA/Programs/Python/Python314/Scripts:$LOCALAPPDATA/Microsoft/WinGet/Links:/c/Program Files/GitHub CLI:/c/Program Files/Git/cmd"`
(PowerShell equivalent: `$env:PYTHONUTF8='1'` — the tools above are already on the user PATH for new shells.)

## 0. Is a new batch needed?
`git pull --rebase`, then `python scripts/daily/daily.py buffer` → if `need_batch` is **false**, the queue already holds
7+ days of clips: reply "Queue full (N clips, D days), nothing to do" and **stop**. That saves credit.

## 1. Housekeeping
- `python scripts/daily/daily.py cleanup`: removes leftover staging releases older than 1 day.
- The user keeps no clips on the PC: GitHub (the `clips-<batch>` releases) is the only copy the poster needs.
  Delete any leftover local `youtube-clips/<date>-*` folder whose clips are all in a `clips-<batch>` release
  (check with `gh release view clips-<batch>`), using literal paths only.

## 2. Pick today's video
`python scripts/daily/daily.py pick` → JSON `{id, title, url, duration_min}`. That's the newest
unclipped long-form video (newest first, then the back catalogue). If it returns `error`, message the user and stop.

## 3. Download
`mkdir -p youtube-clips/source && cd youtube-clips/source && python ../../scripts/download_video.py <url>`
(prefers H.264, retries automatically; run it in the background and wait, since it can take 10+ min).

## 4. Choose 5-7 clips (this is the important creative step)
Read the **whole** transcript with word timings: `python scripts/shorts/words.py <vtt> <start> <end>`
(dump it in ~10-minute windows). Choose the strongest standalone moments using
SHORTS_EDITING.md §2. Daily rules:
- **25-45 s each** (hard max 60 s). Shorts that short get far better retention.
- **Hook on the first word:** a punchline, confession, absurd claim or question. No greetings or filler.
- Must make sense with zero context and land a payoff. Use multiple `segments` to cut tangents.
- Prefer stories, confessions, hot takes and arguments. Skip sponsor reads, intros and Patreon plugs.
- Spread picks across the episode; no two clips about the same story.
- Score each 0-100 and rank. Make 7 if there are 7 good ones, but never pad below quality: 5 is the minimum.

## 5. Write the plan
`daily/plans/<YYYY-MM-DD>-<video_id>.plan.json` (tracked in git; `youtube-clips/` is ignored), with `output_dir` set to `youtube-clips/<YYYY-MM-DD>-<video_id>`.
Per clip: segments, 2-4 emphasis zooms, 3-6 highlight words, `corrections` for misspelled names,
plus these extra fields, which the publish step uses:
- `yt_title`: 40-60 characters, curiosity hook, at most one emoji (e.g. "He Joined a Band to Get Girls… It Backfired 😭")
- `ig_caption`: hook line + a question + 3-5 hashtags
- `pinned_comment`: a question that invites replies
- `tags`: 5-8 YouTube tags

## 6. Speakers
`python scripts/shorts/make_short.py --plan <plan> --analyze-only`. Open every
`_work/<id>/people.png`, match faces to who is talking in the transcript, and set `main_speaker`
(plus `focus` ranges for important lines from others).

## 7. Render
`python scripts/shorts/make_short.py --plan <plan> --keep-work` in the background
(about 5-8 min per 30-45 s clip on this PC). Wait for `report:`.

## 8. Quality control
Every clip's metadata JSON `qc.passed` must be true. **Open every `_work/<id>/contact_sheet.png`**
and check: the speaker is framed, captions are readable and not overlapping, and there are no wrong-person shots during key lines.
Fix the plan and re-render with `--only <id>`. Drop a clip rather than ship a bad one.

## 9. Stage
`python scripts/daily/daily.py stage <output_dir>/clips <YYYY-MM-DD>-<video_id>` → public URLs.

## 10. Publish to YouTube drafts, queue, notify
Build the payload JSON and save it locally as `<output_dir>/payload.json`:
```json
{"telegram_chat_id": <YOUR_TELEGRAM_CHAT_ID>, "privacy": "private", "category_id": "24",
 "source_id": "<video_id>", "source_title": "<video title>",
 "clips": [{"id": "clip_01", "file": "clip_01.mp4", "staging_url": "<url>",
            "yt_title": "...", "yt_description": "<title>\n\n😂 Follow for daily anime & gaming podcast moments → @YourChannel\n\n🎙️ Clip from: <video title> — <video url>\n\n#shorts #anime #podcast #weeb #funny",
            "tags": [...], "ig_caption": "...", "pinned_comment": "...", "duration": 38, "score": 88}]}
```
Order clips by score. Then run `daily/composio_publish.py` CELL 1, CELL 2 and CELL 3 as three
separate `COMPOSIO_REMOTE_WORKBENCH` calls (replace `__PAYLOAD_JSON__` in CELL 1). Start
with `COMPOSIO_SEARCH_TOOLS` (session `generate_id: true`) and reuse that session id.
Check the CELL 2 output: every clip should get a youtube_id. Retry failures once. If YouTube answers
429 "Video Uploads per day" quota, don't retry: write `{}` (or only the ids that worked) to youtube_ids.json
and carry on. The cloud poster uploads any clip without a youtube_id straight to public at its slot.
Then, locally:
- write `<output_dir>/youtube_ids.json` as `{"clip_01": "<youtube_id>", ...}` from the CELL 2 output
- `python scripts/daily/daily.py release <output_dir>/clips <YYYY-MM-DD>-<video_id>`: uploads the clips to the
  private `clips-<YYYY-MM-DD>-<video_id>` release the cloud poster posts from. Must succeed before enqueueing.
- `git pull --rebase`, then `python scripts/daily/daily.py enqueue <output_dir>/payload.json <output_dir>/youtube_ids.json <output_dir>/clips <YYYY-MM-DD>-<video_id>`
- `python scripts/daily/daily.py unstage <YYYY-MM-DD>-<video_id>`: the files have reached YouTube, so remove the public copy now.
CELL 3 sends only a short "batch ready" summary. Publishing happens later in the cloud, one clip per posting slot.

## 11. Finish
- `python scripts/daily/daily.py mark <video_id> <number_of_clips>`
- Once the release, enqueue and push for this video have succeeded, delete the whole local batch folder
  `youtube-clips/<YYYY-MM-DD>-<video_id>` and `youtube-clips/source/<video_id>.*` (literal paths, never globs on
  the parent folder). The user wants nothing kept on the PC; GitHub holds the clips.
- Commit `daily/state.json`, `daily/queue.json` and the plan file (not the videos), then `git pull --rebase` and push
  **right away, after every video** (not only at the end): the cloud poster only sees pushed clips.
- Then go back to step 0 for the next video (see the top of this file for the limit).

## Cloud poster timers (reference)
The poster (`.github/workflows/post.yml`) is triggered by GitHub's cron and by an external **cron-job.org**
job (POST `https://api.github.com/repos/YOUR_GITHUB_USER/your-clipper-repo/actions/workflows/post.yml/dispatches`,
body `{"ref":"main"}`, header `Authorization: Bearer <fine-grained token with Actions: read & write on this repo>`),
every 30 min during 09:00-12:30, 14:00-17:30 and 19:00-22:30 Africa/Lagos. If posts stop, check that job's
history first (an expired token shows as 401).
