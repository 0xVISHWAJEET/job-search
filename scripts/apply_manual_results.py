"""
Applies manually (or alternative-LLM) generated results for listings dumped
by dump_unscored.py - scores, and for qualifying ones, a tailored resume
(from a summary + reordered/trimmed experience list, rendered into
profile/base_resume.tex's own LaTeX macros and compiled), a cover letter,
and Q&A pairs. No Gemini call anywhere in this path.

Input: a JSON Lines file, one object per processed listing, matched back
to jobs/unscored.jsonl by apply_url:
{
  "apply_url": "...",                    # required, must match unscored.jsonl
  "score": 0.92, "reasoning": "...",      # required
  "summary": "...",                       # only if score >= MIN_SCORE
  "experience": [...],                    # only if score >= MIN_SCORE - same
                                           # shape as tailor_cv.py's tailored
                                           # experience array
  "cover_letter": "...",                  # only if score >= MIN_SCORE
  "qa_pairs": [{"question":"...","answer":"...","source":"generated"}]
}

Usage:
    python3 scripts/apply_manual_results.py results.jsonl
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "jobs"))
from jobs_db import JobsDB  # noqa: E402
from tailor_cv import _latex_escape, _render_experience_section, BASE_TEX_PATH, COMPILE_SCRIPT  # noqa: E402
from cover_letter import _slugify  # noqa: E402

ROOT = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "jobs" / "outputs"
UNSCORED_PATH = ROOT / "jobs" / "unscored.jsonl"
MIN_SCORE = 0.80


def _load_unscored_by_url() -> dict:
    by_url = {}
    with UNSCORED_PATH.open() as f:
        for line in f:
            if line.strip():
                entry = json.loads(line)
                by_url[entry["apply_url"]] = entry
    return by_url


def _render_tailored_tex(company: str, role: str, summary: str, experience: list[dict]) -> Path:
    base_tex = BASE_TEX_PATH.read_text()

    summary_pattern = re.compile(r"(%% ── Professional Summary.*?\n)(.*?)(\n\n%% ── Skills)", re.DOTALL)
    new_tex = summary_pattern.sub(
        lambda m: f"{m.group(1)}\n  \\small\n  {_latex_escape(summary)}\n{m.group(3)}",
        base_tex,
    )
    experience_pattern = re.compile(r"(%% ── Experience.*?\\section\{Experience\}\n\n)(.*?)(\n\n%% ── Education)", re.DOTALL)
    new_tex = experience_pattern.sub(
        lambda m: f"{m.group(1)}{_render_experience_section(experience)}{m.group(3)}",
        new_tex,
    )

    out_name = f"tailored_{_slugify(company)}_{_slugify(role)}"
    tex_path = OUTPUT_DIR / f"{out_name}.tex"
    tex_path.write_text(new_tex)
    return tex_path


def apply_one(db: JobsDB, listing: dict, result: dict) -> None:
    company = listing["company"] or listing["portal"]
    role = listing["title"]
    # This is also used for backfill on already-scored jobs (see
    # dump_pending_backfill.py) - fall back to the existing DB score/
    # reasoning rather than requiring the caller to re-supply them.
    existing = db.get_by_apply_url(listing["apply_url"])
    score = result.get("score", existing["match_score"] if existing else None)
    reasoning = result.get("reasoning", existing["match_reasoning"] if existing else "")

    job_id = db.add_or_update(
        portal=listing["portal"], title=listing["title"], company=listing["company"],
        role_category=listing["role_category"], location=listing["location"],
        apply_url=listing["apply_url"], posted_date=listing["posted_date"],
        match_score=score, match_reasoning=reasoning,
        status="scored",
    )

    if score < MIN_SCORE:
        return

    fields = dict(
        portal=listing["portal"], title=listing["title"], company=listing["company"],
        role_category=listing["role_category"], location=listing["location"],
        apply_url=listing["apply_url"], posted_date=listing["posted_date"],
        match_score=score, match_reasoning=result.get("reasoning", ""),
        status="tailored",
    )

    if result.get("summary") and result.get("experience"):
        try:
            tex_path = _render_tailored_tex(company, role, result["summary"], result["experience"])
            subprocess.run([str(COMPILE_SCRIPT), str(tex_path)], check=True, capture_output=True)
            fields["tailored_resume_path"] = str(tex_path.with_suffix(".pdf"))
        except Exception as exc:
            print(f"    (resume render/compile failed for {role!r} @ {company!r}: {exc})", file=sys.stderr)

    if result.get("cover_letter"):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cl_path = OUTPUT_DIR / f"cover_letter_{_slugify(company)}_{_slugify(role)}.md"
        cl_path.write_text(result["cover_letter"].strip() + "\n")
        fields["cover_letter_path"] = str(cl_path)

    db.add_or_update(**fields)

    if result.get("qa_pairs"):
        db.replace_questions(job_id, result["qa_pairs"])


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} results.jsonl", file=sys.stderr)
        sys.exit(1)

    results_path = Path(sys.argv[1])
    by_url = _load_unscored_by_url()
    db = JobsDB()

    applied = 0
    skipped = 0
    with results_path.open() as f:
        for line in f:
            if not line.strip():
                continue
            result = json.loads(line)
            listing = by_url.get(result["apply_url"])
            if not listing:
                print(f"  (no matching unscored listing for {result['apply_url']} - skipped)", file=sys.stderr)
                skipped += 1
                continue
            apply_one(db, listing, result)
            applied += 1

    print(f"Applied {applied} results ({skipped} unmatched/skipped).")
    print(json.dumps(db.summary(), indent=2))


if __name__ == "__main__":
    main()
