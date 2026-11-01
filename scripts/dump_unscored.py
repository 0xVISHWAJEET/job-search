"""
Runs the full discovery aggregation (same sources as discover_jobs.py:
RemoteOK, WWR, web3.career, Dice, Remotive, Wellfound, BuiltIn, GitJobs,
JustJoinIT, LinkedIn, curated ATS boards, career-ops, freehire.me) and the
same description-fetch + posting_guard cleaning pass - but makes NO LLM
call. Anything not already scored in jobs.db gets written to a JSON Lines
file for manual/alternative-LLM processing (see scripts/apply_manual_results.py).

Usage:
    python3 scripts/dump_unscored.py [output.jsonl]
"""
import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent / "jobs"))
from jobs_db import JobsDB  # noqa: E402
from discover_jobs import (  # noqa: E402
    discover_remoteok, discover_wwr, discover_web3career, discover_dice,
    discover_remotive, discover_wellfound, discover_builtin, BUILTIN_HOSTS,
    discover_gitjobs, discover_justjoinit, discover_linkedin, LINKEDIN_KEYWORDS,
    discover_all_ats_boards, discover_via_career_ops, discover_via_freehire,
    _classify_role, _fetch, MAX_AGE_DAYS,
)
from posting_guard import guard as guard_posting  # noqa: E402


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "jobs" / "unscored.jsonl"
    db = JobsDB()

    print("Discovering...")
    all_listings = []
    all_listings += discover_remoteok()
    all_listings += discover_wwr()
    all_listings += discover_web3career()
    all_listings += discover_dice(["devops", "site reliability", "blockchain", "cryptography"])
    all_listings += discover_remotive()
    all_listings += discover_wellfound()
    all_listings += discover_builtin(
        ["/jobs/dev-ops", "/jobs/blockchain-crypto", "/jobs/artificial-intelligence"],
        hosts=BUILTIN_HOSTS,
    )
    all_listings += discover_gitjobs()
    all_listings += discover_justjoinit(["devops", "ai", "security", "go"])
    all_listings += discover_linkedin(LINKEDIN_KEYWORDS)
    all_listings += discover_all_ats_boards(_classify_role, MAX_AGE_DAYS)
    all_listings += discover_via_career_ops(_classify_role, MAX_AGE_DAYS)
    all_listings += discover_via_freehire(_classify_role, MAX_AGE_DAYS)
    print(f"Discovered {len(all_listings)} listings total.")

    seen_urls = set()
    written = 0
    with out_path.open("w") as f:
        for listing in all_listings:
            if listing["apply_url"] in seen_urls:
                continue
            seen_urls.add(listing["apply_url"])

            existing = db.get_by_apply_url(listing["apply_url"])
            if existing and existing["match_score"] is not None:
                continue  # already scored - same skip rule as discover_jobs.py

            description = listing.get("description") or listing["title"]
            if (listing["portal"] in ("LinkedIn", "Dice", "Wellfound", "GitJobs", "JustJoinIT")
                    or listing["portal"].startswith("BuiltIn")
                    or listing["portal"].startswith("CareerOps:")) and not description.strip():
                try:
                    html = _fetch(listing["apply_url"])
                    soup = BeautifulSoup(html, "html.parser")
                    if listing["portal"] == "LinkedIn":
                        desc_el = soup.select_one(".description__text, .show-more-less-html__markup")
                        if desc_el:
                            description = desc_el.get_text(separator="\n", strip=True)
                    elif listing["portal"] == "Dice":
                        ld_el = soup.select_one("[data-testid=jobDetailStructuredData]")
                        if ld_el and ld_el.string:
                            ld_data = json.loads(ld_el.string)
                            raw_desc = ld_data.get("description", "")
                            description = BeautifulSoup(raw_desc, "html.parser").get_text(separator="\n", strip=True)
                    elif listing["portal"] == "GitJobs":
                        for tag in soup(["script", "style"]):
                            tag.decompose()
                        description = soup.get_text(separator="\n", strip=True)
                    else:
                        for tag in soup(["script", "style", "nav", "footer", "header"]):
                            tag.decompose()
                        description = soup.get_text(separator="\n", strip=True)[:8000]
                except Exception:
                    pass

            try:
                description, guard_findings = guard_posting(description, "auto")
            except Exception:
                pass

            listing["description"] = description
            f.write(json.dumps(listing) + "\n")
            written += 1

    print(f"Wrote {written} unscored listings to {out_path}")


if __name__ == "__main__":
    main()
