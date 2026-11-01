"""
Fetches descriptions for qualifying jobs still missing a tailored resume,
cover letter, or Q&A, and writes them to jobs/unscored.jsonl in the same
format apply_manual_results.py expects (keyed by apply_url). No LLM call -
just the HTTP fetch, reusing backfill_tailor.py's extraction logic.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jobs"))
from jobs_db import JobsDB  # noqa: E402
from backfill_tailor import _extract_description  # noqa: E402

MIN_SCORE = 0.80
OUT_PATH = Path(__file__).parent.parent / "jobs" / "unscored.jsonl"


def main():
    db = JobsDB()
    qualifying = db.qualifying(min_score=MIN_SCORE)
    pending = [
        j for j in qualifying
        if not j["tailored_resume_path"] or not j["cover_letter_path"] or not db.get_questions(j["id"])
    ]

    written = 0
    with OUT_PATH.open("w") as f:
        for job in pending:
            try:
                description = _extract_description(job["portal"], job["apply_url"])
            except Exception as exc:
                print(f"  skip {job['title']} @ {job['company']}: fetch failed ({exc})", file=sys.stderr)
                continue
            if not description.strip():
                print(f"  skip {job['title']} @ {job['company']}: empty description", file=sys.stderr)
                continue

            f.write(json.dumps({
                "id": job["id"],
                "portal": job["portal"], "title": job["title"], "company": job["company"],
                "role_category": job["role_category"], "location": job["location"],
                "apply_url": job["apply_url"], "posted_date": job["posted_date"],
                "description": description,
                "needs_resume": not job["tailored_resume_path"],
                "needs_cover_letter": not job["cover_letter_path"],
                "needs_qa": not db.get_questions(job["id"]),
            }) + "\n")
            written += 1

    print(f"Wrote {written}/{len(pending)} pending jobs to {OUT_PATH}")


if __name__ == "__main__":
    main()
