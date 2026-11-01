"""
One-off backfill: for every job in jobs.db that already scored >= 0.80,
makes sure it has both a tailored resume and Q&A - whichever of the two
it's missing (older discover_jobs.py runs either hit a Docker-mount bug
that broke every tailoring call, or predate Q&A generation being wired
into the main pipeline at all - see git history / README). Re-fetches
each JD from its apply_url since discovery didn't persist the full
description text.
"""

import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent / "jobs"))
from jobs_db import JobsDB  # noqa: E402
from generate_qa import generate_qa_for_job, write_qa_markdown  # noqa: E402
from tailor_cv import tailor as tailor_resume  # noqa: E402
from cover_letter import write_cover_letter  # noqa: E402
from discover_jobs import _fetch  # noqa: E402

MIN_SCORE = 0.80


def _extract_description(portal: str, apply_url: str) -> str:
    html = _fetch(apply_url)
    soup = BeautifulSoup(html, "html.parser")

    if portal == "LinkedIn":
        el = soup.select_one(".description__text, .show-more-less-html__markup")
        return el.get_text(separator="\n", strip=True) if el else ""

    # WeWorkRemotely / web3.career listing pages - fall back to full page
    # text if there's no more specific container, good enough for scoring/
    # tailoring purposes (already proven to work for these portals during
    # discovery, where the RSS/table content served the same role).
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:8000]


def main():
    db = JobsDB()
    qualifying = db.qualifying(min_score=MIN_SCORE)
    pending = [
        j for j in qualifying
        if not j["tailored_resume_path"] or not db.get_questions(j["id"]) or not j["cover_letter_path"]
    ]

    print(f"{len(pending)} qualifying jobs need tailoring and/or Q&A "
          f"(out of {len(qualifying)} qualifying total).\n")

    for i, job in enumerate(pending, 1):
        needs_resume = not job["tailored_resume_path"]
        needs_qa = not db.get_questions(job["id"])
        needs_cl = not job["cover_letter_path"]
        print(f"[{i}/{len(pending)}] {job['title']} @ {job['company']} ({job['portal']})"
              f" - resume:{'yes' if needs_resume else 'no'} qa:{'yes' if needs_qa else 'no'}"
              f" cover_letter:{'yes' if needs_cl else 'no'}")

        try:
            description = _extract_description(job["portal"], job["apply_url"])
            if not description.strip():
                print("  skip: could not extract description")
                continue

            if needs_resume:
                pdf_path = tailor_resume(job["company"] or job["portal"], job["title"], description)
                db.add_or_update(
                    portal=job["portal"], title=job["title"], company=job["company"],
                    role_category=job["role_category"], location=job["location"],
                    apply_url=job["apply_url"], posted_date=job["posted_date"],
                    match_score=job["match_score"], match_reasoning=job["match_reasoning"],
                    tailored_resume_path=str(pdf_path), status="tailored",
                )
                print(f"  resume: {pdf_path}")

            if needs_qa:
                qa_pairs = generate_qa_for_job(description)
                qa_path = write_qa_markdown(job["company"] or job["portal"], job["title"], qa_pairs)
                db.replace_questions(job["id"], qa_pairs)
                print(f"  Q&A ({len(qa_pairs)}): {qa_path}")

            if needs_cl:
                cl_path = write_cover_letter(job["company"] or job["portal"], job["title"], description)
                db.add_or_update(
                    portal=job["portal"], title=job["title"], company=job["company"],
                    role_category=job["role_category"], location=job["location"],
                    apply_url=job["apply_url"], posted_date=job["posted_date"],
                    match_score=job["match_score"], match_reasoning=job["match_reasoning"],
                    cover_letter_path=str(cl_path), status="tailored",
                )
                print(f"  cover letter: {cl_path}")

        except Exception as exc:
            print(f"  FAILED: {exc}")

        time.sleep(2)  # stay well under the free-tier per-minute rate limit

    print("\nDone.")
    print(db.summary())


if __name__ == "__main__":
    main()
