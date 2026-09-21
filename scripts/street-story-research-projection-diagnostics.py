#!/usr/bin/env python3
"""Read-only sanitized Street Story research/source projection diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

DB = Path("/home/dev/.local/state/street-story/data/street-story.sqlite3")


def safe_json(raw):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def uniq_urls(items):
    out = []
    seen = set()
    for value in items:
        url = str(value or "").strip()
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    story = db.execute(
        "SELECT id,state,error_code,error_message,research_json,updated_at FROM stories ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if story is None:
        print(json.dumps({"story": None}, sort_keys=True))
        return
    research = safe_json(story["research_json"])
    job = db.execute(
        "SELECT id,state,last_error FROM jobs WHERE story_id=? AND kind IN ('research','refinement') ORDER BY created_at DESC LIMIT 1",
        (story["id"],),
    ).fetchone()
    checkpoint = None
    if job is not None:
        row = db.execute(
            "SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage='grounded_research_v2'",
            (job["id"],),
        ).fetchone()
        if row is not None:
            checkpoint = safe_json(row["value_json"])
    checkpoint = checkpoint or {}
    payload = checkpoint.get("payload") if isinstance(checkpoint.get("payload"), dict) else {}
    facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    grounding_sources = checkpoint.get("grounding_sources") if isinstance(checkpoint.get("grounding_sources"), list) else []
    grounding_supports = checkpoint.get("grounding_supports") if isinstance(checkpoint.get("grounding_supports"), list) else []

    source_urls = uniq_urls(
        s.get("url") for s in grounding_sources if isinstance(s, dict)
    )
    support_urls = uniq_urls(
        s.get("source_url") for s in grounding_supports if isinstance(s, dict)
    )
    claimed_urls = uniq_urls(
        url
        for fact in facts if isinstance(fact, dict)
        for url in (fact.get("source_urls") or [])
    )
    projected = db.execute(
        "SELECT fact_id,evidence_supported,selected,sources_json FROM facts WHERE story_id=? ORDER BY rowid",
        (story["id"],),
    ).fetchall()
    projected_rows = []
    for row in projected:
        sources = safe_json(row["sources_json"])
        if not isinstance(sources, list):
            sources = []
        projected_rows.append({
            "fact_id": row["fact_id"],
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "source_urls": uniq_urls(s.get("url") for s in sources if isinstance(s, dict)),
        })

    print(json.dumps({
        "schema_version": 1,
        "story": {
            "id": story["id"],
            "state": story["state"],
            "error_code": story["error_code"],
            "error_message": story["error_message"],
            "updated_at": story["updated_at"],
            "research_source_count": research.get("source_count"),
        },
        "job": None if job is None else {
            "id": job["id"], "state": job["state"], "last_error": job["last_error"],
        },
        "checkpoint": {
            "fact_count": len(facts),
            "grounding_source_count": len(source_urls),
            "grounding_support_count": len(grounding_supports),
            "grounding_source_urls": source_urls,
            "grounding_support_urls": support_urls,
            "claimed_source_urls": claimed_urls,
            "claimed_intersect_grounding": sorted(set(claimed_urls) & set(source_urls)),
            "claimed_intersect_support": sorted(set(claimed_urls) & set(support_urls)),
        },
        "projected_facts": projected_rows,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
