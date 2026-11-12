"""Write clip_report.md: every generated clip in ranking (score) order."""
import json
from pathlib import Path


def write_report(out_dir, plan):
    out_dir = Path(out_dir)
    metas = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((out_dir / 'metadata').glob('*.json'))]
    metas.sort(key=lambda m: -(m.get('score') or 0))
    v = plan.get('video', {})
    lines = [f"# Clip report: {v.get('title', '')}", '', f"Source: {v.get('url', '')}", '',
             '| Rank | Clip | Score | Length | Source time | QC |', '|---|---|---|---|---|---|']
    for i, m in enumerate(metas, 1):
        qc = 'pass' if m['qc']['passed'] else 'review'
        lines.append(f"| {i} | [{m['title']}](clips/{m['id']}.mp4) | {m.get('score', '')} | "
                     f"{m['duration_sec']:.0f}s | {', '.join(m['source']['segments_display'])} | {qc} |")
    for i, m in enumerate(metas, 1):
        e = m['editing']
        lines += ['', f"## {i}. {m['title']}", '',
                  f"- **File:** `clips/{m['id']}.mp4` · thumbnail `thumbnails/{m['id']}.jpg`",
                  f"- **Score:** {m.get('score')} · **Length:** {m['duration_sec']:.1f}s · "
                  f"**Source:** {', '.join(m['source']['segments_display'])}",
                  f"- **Hook:** {m.get('hook') or '-'}",
                  f"- **Description:** {m.get('description') or '-'}",
                  '', '**Suggested caption**', '', m.get('social_caption') or '-', '',
                  ' '.join(m.get('hashtags', [])), '',
                  f"<sub>Edit: accent {e['accent_color']} ({e['accent_reason']}); {len(e['zoom_moments'])} zoom moments, "
                  f"{len(e['sfx'])} SFX, {e['jump_cuts_masked']} jump cuts masked, {e['silence_removed_sec']}s silence removed, "
                  f"split-screen spans: {len(e['split_screen_spans'])}.</sub>"]
        failed = [k for k, ok in m['qc']['checks'].items() if not ok]
        if failed:
            lines.append(f"\n> QC flagged: {', '.join(failed)}")
    (out_dir / 'clip_report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
