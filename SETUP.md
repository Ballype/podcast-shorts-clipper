# Setup: automatic podcast shorts

Turns long YouTube podcast episodes into vertical shorts (speaker tracking, zooms, animated captions)
and posts them automatically to Instagram Reels + YouTube Shorts, 3 times a day, from GitHub Actions.

## How it works
- **Making clips (your PC, with Claude Code):** `daily/RUNBOOK.md` is the step-by-step job. Claude picks the
  best moments from the transcript, renders them with `scripts/shorts/make_short.py`, uploads them to a
  GitHub release on your repo and adds them to `daily/queue.json`.
- **Posting (cloud, no PC needed):** `.github/workflows/post.yml` runs `scripts/post/poster.py` after each
  posting time in `daily/config.json`, posts the next queued clip, and sends a Telegram confirmation.

## 1. Your copy
Fork or create your own **private** repo from this one (your queue and clip releases will live in it).

## 2. Edit `daily/config.json`
- `channels`: the YouTube channel(s) to clip from
- `clips_repo`: `your-github-user/your-repo` (this repo)
- `staging_repo`: a second, public repo used briefly to hand files to Composio
- `telegram_chat_id`, `instagram_handle`, `youtube_handle`
- `posting.times` / `utc_offset_hours`: when to post, in your timezone

## 3. Tools on your PC
Python 3.12+, ffmpeg, yt-dlp, Deno, GitHub CLI (`gh auth login`), then:
`pip install yt-dlp opencv-python-headless numpy requests pysrt python-dotenv google-auth-oauthlib google-api-python-client`

## 4. Accounts and GitHub secrets (Settings → Secrets and variables → Actions)
| Secret | Where it comes from |
|---|---|
| `COMPOSIO_API_KEY` | composio.dev → project API key (`ak_...`). Connect **Instagram** (Business/Creator account) in the same project. |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | Your own Google Cloud OAuth client (Desktop app, YouTube Data API v3 enabled, consent screen **In production**). Save its JSON as `secrets/client_secret.json` and run `python scripts/post/youtube_auth.py`: it logs you in and saves all three secrets for you. |

Then check everything with: `gh workflow run post.yml -f mode=check`

## 5. Reliable timing (recommended)
GitHub's own scheduler often skips runs on quiet repos. Add a free **cron-job.org** job that POSTs to
`https://api.github.com/repos/<you>/<repo>/actions/workflows/post.yml/dispatches` with body `{"ref":"main"}`
and header `Authorization: Bearer <fine-grained token with Actions: read & write on your repo>`,
every 30 minutes in the few hours after each posting time.

## Notes
- Only post content you have the rights to use or that is allowed under the source creator's clip policy.
- `secrets/` and rendered clips are git-ignored; never commit them.
