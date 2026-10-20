#!/usr/bin/env python3
"""End-to-end local run: their analysis pipeline -> the captured wire protocol.

    pipeline/         evidence, commit groups, episodes        (L0-L3)
    transport.py      scoring session, /v1/messages, upload    (L5)
    this file         glue: episode scoring + results payload  (L4/L5)

Two modes:

    demo   --proxy http://127.0.0.1:8899 --results http://127.0.0.1:8899/api/v1/results
           everything stays on this machine; the mock answers like the real server

    live   point --proxy/--results at your own backend implementing the same
           contract; nothing here talks to YC
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from transport import Transport, TransportError  # noqa: E402
import git_enrich  # noqa: E402

PROMPT_IDS = ["session_narrative", "session_narrative_merge", "decision_classifier", "episode_scoring"]
EPISODE_SYSTEM = HERE / "prompts" / "episode_scoring.txt"
NARRATIVE_SYSTEM = HERE / "prompts" / "session_narrative.txt"
MERGE_SYSTEM = HERE / "prompts" / "session_narrative_merge.txt"


def run_pipeline(project: Path, days: int, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(HERE / "pipeline" / "paxel_reproduction.py"),
        str(project),
        "--days",
        str(days),
        "--output-dir",
        str(outdir),
    ]
    print("[pipeline]", " ".join(cmd[1:]))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-2000:])
        sys.stderr.write(proc.stderr[-2000:])
        raise SystemExit(f"pipeline failed with exit {proc.returncode}")
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "[pipeline] ok")


def load(outdir: Path, name: str, default):
    path = outdir / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def episode_prompt(entry: dict, narratives: dict[str, str] | None = None, git_evidence: dict | None = None) -> str:
    sessions = entry.get("sessions") or []
    lines = [
        f"Episode type: {entry.get('type') or 'session_only'}  Sessions: {len(sessions)}, "
        f"Commit groups: {len(entry.get('commits') or [])}",
        f"Link confidence: {entry.get('link_confidence')}",
    ]
    if entry.get("commits"):
        lines.append("Commits:")
        for commit in entry["commits"][:20]:
            lines.append(f"- {commit.get('sha', '')[:10]} {commit.get('subject', '')}")
    if entry.get("changed_files"):
        lines.append("Changed files: " + ", ".join(entry["changed_files"][:20]))
    for session in sessions:
        prompt = session.get("first_prompt") or {}
        lines.append("## Session " + str(session.get("id")))
        source = session.get("source")
        lines.append(f"Source: {source}")
        if isinstance(prompt, dict) and prompt.get("text"):
            lines.append("First prompt: " + str(prompt["text"])[:600])
        events = session.get("events") or session.get("digest") or []
        for event in events[:80]:
            if isinstance(event, dict):
                role = event.get("role") or event.get("type") or "?"
                text = (
                    event.get("text")
                    or event.get("content")
                    or event.get("summary")
                    or event.get("command")
                    or ""
                )
                if text:
                    lines.append(f"{str(role).upper()}: {str(text)[:800]}")
            elif isinstance(event, str):
                lines.append(event[:800])
    if git_evidence and git_evidence.get("available") and git_evidence.get("groups"):
        keys = {str(s.get("id")) for s in sessions if s.get("id")}
        reviews = git_evidence.get("review_signals") or {}
        hits = [f"{sid}: {', '.join(v[:3])}" for sid, v in reviews.items() if sid in keys]
        lines.append("## Repository Evidence (local git, author-filtered)")
        lines.append(
            f"Commits kept: {git_evidence.get('commits')}; "
            f"skipped other authors: {sum((git_evidence.get('skipped_by_author') or {}).values())}; "
            f"branches: {', '.join(git_evidence.get('branches') or []) or 'none'}"
        )
        if hits:
            lines.append("Review-shaped activity in this episode's sessions: " + "; ".join(hits))
        for group in (git_evidence.get("groups") or [])[:3]:
            lines.append(
                f"- {group.get('group_type')} | {group.get('title', '')[:80]} | "
                f"conf {group.get('link_confidence')} | branches {', '.join(group.get('branches') or [])} | "
                f"+{group.get('insertions', 0)}/-{group.get('deletions', 0)}"
            )
            if group.get("diff_excerpt"):
                lines.append(str(group["diff_excerpt"])[:2000])

    if narratives:
        own = [str(s.get("id")) for s in sessions if s.get("id") in narratives]
        if own:
            lines.append("## Session Narratives")
            for sid in own:
                lines.append(f"### {sid}")
                lines.append(narratives[sid][:6000])
    return "\n".join(lines)


def build_results(
    evidence: dict,
    narratives: dict[str, str],
    git_metrics: dict | None,
    episodes: list[dict],
    scores: list[dict],
    nonces: list[str],
    scoring_session_id: str | None,
    slug: str,
    durations: dict,
) -> dict:
    sessions = evidence.get("sessions") or []
    session_metadata = {}
    for session in sessions:
        if not isinstance(session, dict) or not session.get("id"):
            continue
        session_metadata[session["id"]] = {
            "message_count": session.get("event_count"),
            "session_created_at": session.get("started_at"),
            "session_modified_at": session.get("ended_at"),
            "is_subagent": bool(session.get("parent_id")),
            "agent_type": session.get("source"),
            "session_events": [
                {k: (str(v)[:500] if isinstance(v, str) else v) for k, v in event.items()}
                for event in (session.get("events") or [])[:60]
                if isinstance(event, dict)
            ],
            "models_used": session.get("models_used"),
        }
    session_metadata = {k: {kk: vv for kk, vv in v.items() if vv not in (None, [], {})} for k, v in session_metadata.items()}

    commits = evidence.get("commits") or []
    return {
        "episode_scores": scores,
        "session_summaries": {sid: text[:2000] for sid, text in narratives.items()},
        "session_metadata": session_metadata,
        "git_metrics": (
            {
                "total_commits": git_metrics.get("commits"),
                "branches": git_metrics.get("branches"),
                "author_filter": git_metrics.get("author"),
            }
            if git_metrics
            else {}
        ),
        "git_remote": evidence.get("git_remote"),
        "recent_commits": commits[:50],
        "session_count": len(sessions),
        "subagent_session_count": sum(1 for s in sessions if s.get("parent_id")),
        "project_name": evidence.get("project_name") or (Path(evidence.get("project_path") or ".").name),
        "nonces": nonces,
        "scoring_session_id": scoring_session_id,
        "decisions": [],
        "decisions_status": "not_run",
        "code_quality": {"status": "not_run", "dimensions": {}},
        "client_telemetry": {
            "client_slug": slug,
            "pipeline_duration_s": round(durations.get("pipeline", 0), 2),
            "step_timings": durations.get("steps", []),
            "llm_stats": {
                "total_calls": len(scores),
                "by_model": {},
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            },
            "orphan_recovery_count": 0,
            "system_info": {
                "python_version": sys.version.split()[0],
                "client": "paxel-local",
            },
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
        "_episode_count": len(episodes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a project and upload through the Paxel wire protocol")
    parser.add_argument("project", type=Path)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--proxy", default="http://127.0.0.1:8899", help="LLM proxy base URL")
    parser.add_argument("--results", default="http://127.0.0.1:8899/api/v1/results")
    parser.add_argument("--token", default="local-demo-token")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--limit", type=int, default=5, help="episodes to score")
    parser.add_argument("--out", type=Path, default=Path("paxel-local-out"))
    parser.add_argument("--reuse", action="store_true", help="reuse existing pipeline output")
    parser.add_argument("--git-repo", type=Path, help="repository for git evidence (defaults to the project)")
    parser.add_argument("--author-email", help="only credit commits by this author")
    parser.add_argument("--no-enrich-git", action="store_true", help="skip local git enrichment")
    parser.add_argument("--all-narratives", action="store_true", help="narrate every session, not just scored episodes")
    args = parser.parse_args()

    started = time.time()
    outdir = args.out.resolve()
    if not args.reuse:
        run_pipeline(args.project.resolve(), args.days, outdir)
    durations = {"pipeline": time.time() - started}

    evidence = load(outdir, "evidence.json", {})
    episodes = load(outdir, "episodes.json", [])
    episode_inputs = load(outdir, "episode_inputs.json", {})
    slug = uuid.uuid4().hex[:8]

    git_evidence: dict = {"available": False, "reason": "disabled"}
    if not args.no_enrich_git:
        repo = (args.git_repo or args.project).resolve()
        git_evidence = git_enrich.enrich(repo, evidence, args.days, args.author_email)
        if git_evidence.get("available"):
            git_enrich.write(git_evidence, outdir)
            print(
                f"[git] {git_evidence['commits']} commits kept, "
                f"{sum((git_evidence.get('skipped_by_author') or {}).values())} skipped, "
                f"{len(git_evidence.get('groups') or [])} groups, "
                f"{len(git_evidence.get('review_signals') or {})} sessions with review signals"
            )
        else:
            print(f"[git] skipped: {git_evidence.get('reason')}")

    transport = Transport(
        proxy_url=args.proxy, results_url=args.results, token=args.token, client_version="paxel-local-0.1"
    )

    print("[protocol] health / validate / preflight")
    transport.health()
    transport.validate()
    preflight = transport.preflight(PROMPT_IDS)
    print("[protocol] contract:", preflight.get("contract_version"))

    session_id = transport.open_scoring_session(evidence.get("git_remote"))
    print("[protocol] scoring session:", session_id)

    manifest = {
        str(ep.get("id")): [link.get("session_id") for link in (ep.get("session_links") or []) if link.get("session_id")]
        for ep in episodes
    }
    transport.commit_manifest(manifest)
    print(f"[protocol] manifest committed: {len(manifest)} episodes")

    narrative_system = NARRATIVE_SYSTEM.read_text(encoding="utf-8")
    merge_system = MERGE_SYSTEM.read_text(encoding="utf-8")
    narrative_inputs = load(outdir, "narrative_inputs.json", {})
    # Only narrate sessions that belong to the episodes we are about to score,
    # unless the caller asks for every session.
    if not args.all_narratives:
        wanted = set()
        for entry in list(episode_inputs.values())[: args.limit]:
            for session in entry.get("sessions") or []:
                if session.get("id"):
                    wanted.add(str(session["id"]))
        if wanted:
            narrative_inputs = {k: v for k, v in narrative_inputs.items() if k in wanted}
    narratives: dict[str, str] = {}
    for index, (narr_session_id, entry) in enumerate(narrative_inputs.items(), 1):
        parts = [part for part in (entry.get("parts") or []) if part.strip()]
        if not parts:
            continue
        texts = []
        for part_index, part in enumerate(parts, 1):
            try:
                reply = transport.messages(
                    prompt_id="session_narrative",
                    user_content=part,
                    model=args.model,
                    max_tokens=1200,
                    session_id=narr_session_id,
                    system=narrative_system,
                )
            except TransportError as exc:
                print(f"[narrative] {narr_session_id} part {part_index} failed: {exc}")
                continue
            texts.append(reply["text"].strip())
        if not texts:
            continue
        if len(texts) > 1:
            try:
                merged = transport.messages(
                    prompt_id="session_narrative_merge",
                    user_content="\n\n---\n\n".join(texts),
                    model=args.model,
                    max_tokens=1200,
                    session_id=narr_session_id,
                    system=merge_system,
                )
                texts = [merged["text"].strip()]
            except TransportError as exc:
                print(f"[narrative] {narr_session_id} merge failed: {exc}, keeping first part")
                texts = texts[:1]
        narratives[narr_session_id] = texts[0]
        print(
            f"[narrative] {index}/{len(narrative_inputs)} {narr_session_id} "
            f"({len(parts)} part(s) -> {len(narratives[narr_session_id])} chars)"
        )

    system_prompt = EPISODE_SYSTEM.read_text(encoding="utf-8")
    scores = []
    for index, (episode_id, entry) in enumerate(list(episode_inputs.items())[: args.limit], 1):
        prompt = episode_prompt(entry, narratives, git_evidence)
        print(f"[score] {index}/{min(len(episode_inputs), args.limit)} {episode_id} ({len(prompt)} chars)")
        try:
            reply = transport.messages(
                prompt_id="episode_scoring",
                user_content=prompt,
                model=args.model,
                max_tokens=1200,
                session_id=(entry.get("sessions") or [{}])[0].get("id"),
                system=system_prompt,
            )
        except TransportError as exc:
            print(f"[score] failed: {exc}")
            continue
        text = reply["text"]
        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
        if not parsed or not isinstance(parsed.get("scores"), dict):
            print("[score] no parsable scores, skipping")
            continue
        link = (episodes[[e.get("id") for e in episodes].index(episode_id)].get("session_links") or [None])[0] if episodes else None
        scores.append(
            {
                "episode_id": episode_id,
                "request_id": transport.nonces[-1].split(":")[0] if transport.nonces else None,
                "envelope": None,
                "title": parsed.get("title"),
                "type": entry.get("type"),
                "scores": parsed["scores"],
                "confidence": parsed.get("confidence"),
                "session_ids": [
                    {
                        "session_id": (s or {}).get("session_id"),
                        "link_type": (s or {}).get("link_type"),
                        "link_confidence": (s or {}).get("link_confidence"),
                    }
                    for s in ([link] if link else (episode_inputs.get(episode_id, {}).get("sessions") or []))
                    if s
                ],
            }
        )

    durations["steps"] = [
        {"step": "pipeline", "duration_s": round(durations["pipeline"], 2), "status": "ok"},
        {"step": "episode scoring", "duration_s": round(time.time() - started - durations["pipeline"], 2), "status": "ok"},
    ]

    payload = build_results(evidence, narratives, git_evidence, episodes, scores, transport.nonces, session_id, slug, durations)
    payload.pop("_episode_count", None)

    print(f"[upload] {len(scores)} episodes, {len(transport.nonces)} nonces, {len(payload['session_metadata'])} sessions")
    result = transport.upload_results(payload)
    print(f"[upload] HTTP {result['status']} slug={result['slug']} gzip={result['compressed_bytes']}B "
          f"json={result['payload_bytes']}B replay={result['idempotent_replay']}")

    summary = {
        "project": str(args.project.resolve()),
        "scoring_session_id": session_id,
        "episodes_total": len(episodes),
        "episodes_scored": len(scores),
        "nonces": transport.nonces,
        "upload": result,
        "calls": transport.calls,
        "payload_keys": sorted(payload.keys()),
        "payload_path": str(outdir / "results-payload.json"),
    }
    (outdir / "results-payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done]", outdir / "run-summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
