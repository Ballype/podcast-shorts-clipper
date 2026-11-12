# Vertical Shorts Editing: Reference

How the skill turns a long video into publication-ready 9:16 shorts. Code lives in
`scripts/shorts/`. The single entry point is `make_short.py --plan plan.json`.

## 1. Pipeline and what each feature really does

| Stage | Module | How it works | Status |
|---|---|---|---|
| Word timing | `words.py` | Reads the per-word timestamps YouTube embeds in auto-captions (`<c>` tags), including single-word lines YouTube writes without tags. Bleeps become `****`. Keeps YouTube's `>>` speaker-change markers as turn flags. | Implemented. If the subtitles are manual (no word tags), times are **estimated** per cue and a NOTE is printed. Whisper is not used. |
| Segment cut + silence trim | `prepare.py` | Cuts the plan's segments, shortens silences ≥0.55 s (`silencedetect`), never cuts through a spoken word, and keeps a time map so captions, zooms and SFX stay in sync. | Implemented |
| Shot detection | `faces.py` | FFmpeg scene detection at full frame rate. | Implemented |
| Face detection + tracking | `faces.py` | OpenCV YuNet (`assets/models/face_detection_yunet_2023mar.onnx`) at 6 fps, plus a per-shot track table that tolerates missed detections. Falls back to a Haar cascade if the model is missing. | Implemented |
| Who is who | `camera.py` | Groups face tracks into people across camera cuts by clothing colour (HSV histogram of the chest). Tracks on screen at the same time are never merged. A strict threshold means uncertain matches stay separate: the same person may appear as two IDs, but two people are rarely merged. | Implemented |
| Active speaker | `faces.py`, `camera.py` | Priority order: **(1)** Claude's `main_speaker` / `focus` override from the people sheet; **(2)** audio speaker turns (`>>` markers; interjections under 1 s or 3 words are merged), each assigned to the person with the highest *median* mouth motion; **(3)** frame-level mouth motion with hysteresis. | **(2) and (3) are heuristics.** They fail when a mic covers the speaker's mouth or a listener touches their face. That's why step 5b of the workflow (the people sheet) exists. |
| Vertical reframing | `camera.py`, `render.py` | 9:16 virtual camera follows the active face, with a deadzone so it doesn't jitter, smooth easing on speaker changes and hard snaps on camera cuts. Wide shots get a base zoom so the face is about 20% of the frame width (max 1.4x). Rendering uses sub-pixel affine warps. | Implemented |
| Split screen | `camera.py` | When two visible people trade lines rapidly (≥3 speaker switches in 8 s), the frame stacks both faces top and bottom. | Implemented (`split_screen: auto/off/always`) |
| Zooms | `camera.py` | A hook push-in over the first 2 s. Emphasis zooms ease in 0.3 s before the statement, hold, and ease out: subtle 1.08x, medium 1.18x, strong 1.32x, scaled by the `zoom` style. Capped so the face is never more than about 38% of the width. Jump cuts from silence trimming alternate +6% zoom to hide the jump. | Implemented. Emphasis moments come from the plan (Claude picks them from the transcript). |
| Captions | `captions.py` | ASS rendered by libass: 2-3 words per caption, each word appears as it is spoken (the line never reflows), phrase pop-in (82→106→100% scale), uppercase Arial Black with a thick outline, lower-middle position. | Implemented |
| Highlights | `captions.py` | Words listed in the plan, plus numbers and money words automatically, get the accent colour. | Implemented |
| Accent colour | `captions.py` | User colour → readable accent colour from the footage → yellow `#FFD400`. A vivid hue that covers more than 4% of the frame is treated as set lighting (it would blend in) and rejected. The reason is written to metadata. | Implemented. It cannot recognise logos or brands; pass `caption_color` for a brand colour. |
| SFX | `sfx.py`, `camera.py` | Whoosh on medium/strong zooms, soft impact on strong ones, at most one per 2 s, mixed about 4 dB under the average dialogue level (`sfx_whoosh_db` / `sfx_impact_db` adjust it). Default sounds are **synthesised**; put your own `whoosh.wav` / `impact.wav` in `assets/sfx/custom/` to override them. | Implemented |
| Audio cleanup | `render.py` | High-pass at 70 Hz, light FFT denoise, loudness normalised to -14 LUFS (the platform norm), limiter at -1 dBFS (headroom for AAC overshoot). | Implemented (`keep_original_audio` skips it) |
| QC | `qc.py` | Resolution, codecs, duration, black frames, loudness and peak, framing ratio, captions present. Also writes a contact sheet for visual review. | Implemented. Hook, context and payoff quality are judged by Claude, not by code. |

## 2. Clip selection rules (Claude does this from the transcript)

- **Hook**: start on the first word of the interesting statement, question or story beat. No greetings, filler or "so anyway".
- **Context**: include what's needed to understand the clip on its own. Use extra `segments` to drop a tangent instead of cutting context.
- **Payoff**: the clip must land the punchline, reveal or conclusion. Never end mid-sentence.
- **Ending**: end about 0.1-0.5 s after the payoff's last word. Use `words.py` to get exact word times.
- **Length**: 20-60 s by default, 90 s max, unless the user sets a limit.
- **Score (0-100)**: hook strength 30 + standalone clarity 25 + payoff 25 + shareability/emotion 20.
- **Cut points**: segment boundaries should sit between words (check with `words.py`). Leave about 0.05 s of margin before the first word.

## 3. plan.json schema

```json
{
  "video": {"title": "...", "url": "https://youtu.be/...",
            "source": "youtube-clips/source/<id>.mp4", "subs": "youtube-clips/source/<id>.en.vtt"},
  "output_dir": "youtube-clips/<video-slug>",
  "options": {"zoom": "normal"},
  "clips": [
    {"id": "clip_01", "title": "Short, hooky title", "score": 92,
     "hook": "first line the viewer hears",
     "segments": [["31:27.40", "31:35.00"], ["32:39.55", "33:57.55"]],
     "emphasis": [{"t": "33:27.04", "end": "33:30.20", "strength": "strong"}],
     "highlight_words": ["bathroom", "stage"],
     "description": "1-2 sentences", "social_caption": "post text", "hashtags": ["#shorts"],
     "options": {}}
  ]
}
```

All times are in **source video time**: seconds or `[HH:]MM:SS(.ms)`. `emphasis.t` is
when the key statement starts; `end` is when it finishes.

Optional fields:
- `"corrections": {"Croninberg": "Kronenbourg"}` fixes transcript words in the captions. It works at plan level or per clip.
- `"main_speaker": 0` makes the camera follow person P0 for the whole clip.
- `"focus": [{"from": "33:20", "to": "33:40", "person": [2, 3]}]` overrides the speaker for a range. It takes priority over `main_speaker`. A list is allowed because one real person can appear as two IDs.

### 3b. Choosing the speaker (people sheet)

Person IDs are only known after face analysis, so run:

```
python scripts/shorts/make_short.py --plan plan.json --analyze-only
```

This cuts each clip, runs face analysis (both cached for the real render), and writes
`_work/<id>/people.png` with a labelled crop of every person (P0, P1, …). Open it, match
the faces to who talks in the transcript (e.g. the storyteller), and add `main_speaker`
or `focus` to each clip. For a story clip, the storyteller as `main_speaker` is almost
always right; add `focus` ranges for important lines from other people. Without overrides
the automatic heuristics are used.

## 4. Options (global `options`, or per-clip `options` to override)

| Option | Default | Values | User says... |
|---|---|---|---|
| `zoom` | `normal` | `off`, `subtle`, `normal`, `strong` | "no zooms", "subtle zooms", "stronger zooms" |
| `sfx` | `true` | bool | "no sound effects" |
| `caption_color` | auto | hex, e.g. `#3BA7FF` | "blue captions", "use my brand colour #..." |
| `caption_size` | `1.0` | scale | "smaller captions" → `0.8` |
| `caption_position` | `lower` | `lower`, `middle`, `upper` | "captions in the middle" |
| `caption_font` | `Arial Black` | installed font name | "use Impact" |
| `caption_uppercase` | `true` | bool | "normal case captions" |
| `words_per_caption` | `3` | 1-4 | "one word at a time" → `1` |
| `split_screen` | `auto` | `auto`, `off`, `always` | "no split screen" |
| `trim_silence` | `true` | bool | "don't cut pauses" |
| `keep_original_audio` | `false` | bool | "keep original audio untouched" |
| `denoise` | `true` | bool | |
| `max_base_zoom` | `1.4` | ≥1 | "show more of the room" → `1.1` |
| `x264_preset` / `crf` | `medium` / `18` | x264 values | use `--fast` for quick drafts |

Other requests map onto selection rather than options:
- "only the best clip": 1 clip.
- "3 clips": 3 clips.
- "under 30 s": choose and trim segments to fit.
- "TikTok" or "Reels": same 9:16 export, and adjust the hashtags and caption tone.
- "more cinematic": `zoom: subtle`, fewer emphasis moments, `sfx: false`, `words_per_caption: 2`.

## 5. Output

```
youtube-clips/<video-slug>/
  clips/clip_01.mp4          1080x1920 H.264/AAC, ready to post
  metadata/clip_01.json      title, score, duration, source timestamps, captions, hashtags, edit log, QC
  thumbnails/clip_01.jpg     frame from the strongest moment
  clip_report.md             all clips, ranked
  _work/clip_01/             faces.json, captions.ass, contact_sheet.png (intermediate video deleted after QC passes)
```

## 6. Performance

On a 2-core PC a 60 s clip takes about 4-6 minutes. Source decode dominates, so download
H.264 rather than AV1 (`download_video.py` now prefers H.264). Use `--fast` for drafts.

## 7. Known limitations

- Active-speaker detection is visual only (no audio diarization). A loud off-camera speaker won't be followed.
- Caption word times come from YouTube auto-captions. Videos with only manual subtitles get estimated word timing.
- A source resolution above 1080p isn't used, so tight zooms on wide shots are upscaled and slightly soft.
- The default SFX are synthetic. Add real ones to `assets/sfx/custom/` for a more polished sound.
