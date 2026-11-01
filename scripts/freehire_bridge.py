"""
Bridge into the vendored ai-job-search freehire-search skill
(vendor/ai-job-search/freehire-search/), a zero-dependency Bun/TypeScript CLI
that queries freehire.me's public JSON API - ~50 ATS platforms aggregated
into one schema, across many countries and remote, tech-focused. No auth,
no scraping workaround, no bot-detection bypass: a public REST endpoint.

Each search result already carries the posting's full description (no
per-listing follow-up fetch needed, unlike LinkedIn/Dice/etc.).

Usage (standalone):
    python3 freehire_bridge.py --query "devops" --jobage 30 --limit 50

As a library (used by discover_jobs.py):
    from freehire_bridge import discover_via_freehire
    listings = discover_via_freehire(classify_role_fn, max_age_days=30, queries=["devops", "blockchain"])
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

CLI_DIR = Path(__file__).parent.parent / "vendor" / "ai-job-search" / "freehire-search" / "cli"

# Fallback only - discover_jobs.py's main() passes the user's own
# preferences.target_roles (from candidate_profile.json, via
# setup_profile.py) as `queries` explicitly. This default is just for
# standalone/library use before a profile exists.
DEFAULT_QUERIES = ["devops", "site reliability", "platform engineer", "blockchain", "ai engineer", "security engineer"]

# 25, not freehire-search's own default of 25/CLI max of 50+ - empirically,
# requesting 50 full-description results in one call reliably truncates
# Python subprocess.run's captured stdout well before the JSON closes
# (reproduced directly, independent of network conditions: content-length-
# dependent, not a timeout or connectivity issue). 25 was stable across
# repeated runs; this trades fewer results per query for actually getting
# valid JSON back every time.
DEFAULT_LIMIT = 25


def _bun_path() -> str:
    bun = shutil.which("bun")
    if bun:
        return bun
    # bun's installer adds ~/.bun/bin to PATH via shell rc files, which a
    # non-interactive subprocess environment won't have sourced yet.
    fallback = Path.home() / ".bun" / "bin" / "bun"
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError(
        "bun is not installed or not on PATH - run: curl -fsSL https://bun.sh/install | bash"
    )


def _run_search(query: str, jobage_days: int, limit: int) -> list[dict]:
    """Runs one freehire-search query and returns its raw result objects.
    Raises if the CLI or vendored checkout is missing - callers should catch
    and treat it as "this source produced nothing", same as any other
    discover_*() failure."""
    if not CLI_DIR.exists():
        raise FileNotFoundError(f"freehire-search not vendored at {CLI_DIR}")

    cmd = [
        _bun_path(), "run", "src/cli.ts", "search",
        "-q", query,
        "--jobage", str(jobage_days),
        "--limit", str(limit),
        "--format", "json",
    ]
    # One retry on a transient failure - a one-off run against the live CLI
    # showed occasional truncated JSON (likely a dropped connection to
    # freehire.me mid-response under back-to-back query load, not a
    # reproducible content bug: the same query succeeded cleanly on retry).
    last_exc = None
    for attempt in range(2):
        try:
            proc = subprocess.run(cmd, cwd=CLI_DIR, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired as exc:
            last_exc = RuntimeError(f"freehire-search query {query!r} timed out: {exc}")
            continue

        if proc.returncode != 0:
            # The CLI writes {"error": "...", "code": "..."} to stderr on failure
            # (e.g. freehire.me unreachable) - degrade gracefully, same posture
            # the CLI itself documents.
            last_exc = RuntimeError(f"freehire-search query {query!r} failed: {proc.stderr.strip()[:300]}")
            continue

        try:
            return json.loads(proc.stdout).get("results", [])
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = RuntimeError(f"freehire-search returned non-JSON output: {exc}")
            continue

    raise last_exc


def discover_via_freehire(
    classify_role_fn,
    max_age_days: int = 30,
    queries: list[str] | None = None,
    limit_per_query: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Runs one freehire-search query per entry in `queries` (default:
    DEFAULT_QUERIES) and returns listings shaped like discover_jobs.py's
    other discover_*() functions. De-dupes by posting `id` across queries
    (the same job often matches more than one keyword)."""
    seen_ids = set()
    listings = []

    for query in queries or DEFAULT_QUERIES:
        try:
            results = _run_search(query, max_age_days, limit_per_query)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"  (freehire-search query {query!r} skipped: {exc})", file=sys.stderr)
            continue

        for job in results:
            job_id = job.get("id")
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            title = job.get("title", "")
            listings.append({
                "portal": "freehire",
                "title": title,
                "company": job.get("company", ""),
                "role_category": classify_role_fn(title),
                "location": job.get("location") or ", ".join(job.get("countries", []) or []) or job.get("work_mode", ""),
                "apply_url": job.get("url", ""),
                "posted_date": job.get("date", ""),
                "description": job.get("description", ""),
            })

    return listings


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Standalone freehire-search bridge CLI")
    parser.add_argument("--query", action="append", dest="queries", help="repeatable; defaults to a broad tech sweep")
    parser.add_argument("--jobage", type=int, default=30)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    listings = discover_via_freehire(
        lambda title: "unclassified",
        max_age_days=args.jobage,
        queries=args.queries,
        limit_per_query=args.limit,
    )
    print(json.dumps({"count": len(listings), "listings": listings[:5]}, indent=2))
    print(f"\n{len(listings)} total listings (showing first 5 above)", file=sys.stderr)


if __name__ == "__main__":
    main()
