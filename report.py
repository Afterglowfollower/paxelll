"""Standalone HTML report for a finished run.

The upstream pipeline renders its own report *before* narratives and scores
exist, so that file looks empty in protocol mode. This module renders the run
after scoring: one card per scored episode, its narrative, the axis scores, and
the git evidence that fed the judge.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

AXES = [
    ("execution_leverage", "Execution leverage"),
    ("steering", "Steering"),
    ("engineering_quality", "Engineering quality"),
    ("product_thinking", "Product thinking"),
    ("planning", "Planning"),
]

CSS = """
:root { color-scheme: light dark; --fg:#111; --muted:#666; --bg:#fff; --card:#f7f7f8; --line:#e3e3e6; --accent:#3b6ef5; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8ea; --muted:#9a9aa2; --bg:#111114; --card:#1a1a1f; --line:#2c2c33; --accent:#7aa2ff; }
}
* { box-sizing: border-box; }
body { margin:0; padding:32px; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:16px; margin:28px 0 12px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
.stats { display:flex; gap:10px; flex-wrap:wrap; margin:16px 0 8px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:120px; }
.stat b { display:block; font-size:18px; }
.stat span { color:var(--muted); font-size:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin:12px 0; }
.card h3 { margin:0 0 6px; font-size:15px; }
.meta { color:var(--muted); font-size:12px; margin-bottom:10px; }
.axis { display:grid; grid-template-columns:150px 1fr 42px; gap:8px; align-items:center; margin:4px 0; font-size:13px; }
.bar { height:7px; background:var(--line); border-radius:4px; overflow:hidden; }
.bar i { display:block; height:100%; background:var(--accent); }
details { margin-top:10px; }
summary { cursor:pointer; color:var(--accent); font-size:13px; }
p.prose { margin:10px 0; font-size:14px; }
pre.narr { white-space:pre-wrap; word-break:break-word; background:var(--bg); border:1px solid var(--line);
  border-radius:8px; padding:12px; font-size:13px; max-height:420px; overflow:auto; }
.muted { color:var(--muted); }
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def axis_rows(scores: dict) -> str:
    rows = []
    for key, label in AXES:
        if key not in (scores or {}):
            rows.append(
                f'<div class="axis"><span>{esc(label)}</span>'
                f'<span class="muted">not scored (insufficient evidence)</span><span></span></div>'
            )
            continue
        value = float(scores[key])
        rows.append(
            f'<div class="axis"><span>{esc(label)}</span>'
            f'<span class="bar"><i style="width:{max(0, min(100, value * 10)):.0f}%"></i></span>'
            f"<span>{value:.1f}</span></div>"
        )
    return "".join(rows)


def render(outdir: Path) -> Path:
    payload = json.loads((outdir / "results-payload.json").read_text(encoding="utf-8"))
    summary_path = outdir / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    git_path = outdir / "git-enriched.json"
    git_evidence = json.loads(git_path.read_text(encoding="utf-8")) if git_path.exists() else {}

    scores = payload.get("episode_scores") or []
    narratives = payload.get("session_summaries") or {}
    sidecar = outdir / "narratives.json"
    if sidecar.exists():
        try:
            narratives = {**narratives, **json.loads(sidecar.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            pass
    metadata = payload.get("session_metadata") or {}
    nonces = payload.get("nonces") or []

    body = []
    body.append("<h1>AI session audit</h1>")
    body.append(
        f'<div class="sub">project {esc(payload.get("project_name"))} · '
        f'generated {esc(datetime.now().strftime("%Y-%m-%d %H:%M"))} · '
        f'scoring session {esc(payload.get("scoring_session_id"))}</div>'
    )
    body.append('<div class="stats">')
    for label, value in [
        ("sessions", payload.get("session_count")),
        ("episodes scored", len(scores)),
        ("episodes manifested", summary.get("episodes_total")),
        ("nonces", len(nonces)),
        ("upload", (summary.get("upload") or {}).get("status")),
        ("gzip", f'{(summary.get("upload") or {}).get("compressed_bytes", 0) // 1024} KB'),
    ]:
        body.append(f'<div class="stat"><b>{esc(value if value is not None else "-")}</b><span>{esc(label)}</span></div>')
    body.append("</div>")

    if git_evidence.get("available"):
        skipped = sum((git_evidence.get("skipped_by_author") or {}).values())
        body.append("<h2>Repository evidence</h2>")
        body.append(
            f'<div class="meta">commits kept {esc(git_evidence.get("commits"))} · '
            f"skipped other authors {esc(skipped)} · "
            f"branches {esc(', '.join(git_evidence.get('branches') or []) or 'none')} · "
            f"groups {esc(len(git_evidence.get('groups') or []))}</div>"
        )
        for group in (git_evidence.get("groups") or [])[:8]:
            body.append(
                f'<div class="card"><h3>{esc(group.get("title"))}</h3>'
                f'<div class="meta">{esc(group.get("group_type"))} · confidence {esc(group.get("link_confidence"))} · '
                f'branches {esc(", ".join(group.get("branches") or []) or "unknown")} · '
                f'+{esc(group.get("insertions", 0))}/-{esc(group.get("deletions", 0))}</div></div>'
            )

    body.append("<h2>Scored episodes</h2>")
    if not scores:
        body.append(
            '<div class="card muted">No episode was scored in this run. '
            "Raise <code>--limit</code>, or check that the project has analyzable sessions.</div>"
        )
    for score in scores:
        session_ids = [s.get("session_id") for s in (score.get("session_ids") or []) if s.get("session_id")]
        body.append('<div class="card">')
        body.append(f'<h3>{esc(score.get("title") or score.get("episode_id"))}</h3>')
        body.append(
            f'<div class="meta">{esc(score.get("type"))} · confidence {esc(score.get("confidence"))} · '
            f'request {esc(score.get("request_id") or "-")} · sessions {esc(", ".join(session_ids) or "-")}</div>'
        )
        body.append(axis_rows(score.get("scores") or {}))

        assessment = score.get("assessment") or {}
        if assessment.get("facts"):
            body.append(f'<p class="prose"><b>What happened.</b> {esc(assessment["facts"])}</p>')
        if assessment.get("interpretation"):
            body.append(f'<p class="prose"><b>What it suggests.</b> {esc(assessment["interpretation"])}</p>')
        if assessment.get("counterweight"):
            body.append(f'<p class="prose muted"><b>What argues against it.</b> {esc(assessment["counterweight"])}</p>')
        for sid in session_ids:
            narrative = narratives.get(sid)
            if narrative:
                body.append("<details><summary>narrative · " + esc(sid) + "</summary>")
                body.append(f'<pre class="narr">{esc(narrative)}</pre></details>')
            meta = metadata.get(sid) or {}
            if meta:
                body.append(
                    f'<div class="meta">messages {esc(meta.get("message_count"))} · '
                    f'events {esc(len(meta.get("session_events") or []))} · '
                    f'agent {esc(meta.get("agent_type"))}</div>'
                )
        body.append("</div>")

    unscored = max(0, (summary.get("episodes_total") or 0) - len(scores))
    if unscored:
        body.append(
            f'<div class="meta">… plus {unscored} manifested episode(s) not scored in this run '
            f"(raise --limit to include them).</div>"
        )

    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>AI session audit · {esc(payload.get('project_name'))}</title>"
        f"<style>{CSS}</style></head><body>{''.join(body)}</body></html>"
    )
    path = outdir / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out = render(target)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
