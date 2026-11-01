"""
Job discovery across the portals confirmed reachable without a real browser
(see README's "Portal coverage" section for the full picture, including
which of the 196 URLs in URL.txt were tried and excluded, and why).

Sources, each via its own legitimate public interface - no scraping
workarounds, no bot-detection bypass:
  - RemoteOK: public JSON API (https://remoteok.com/api)
  - We Work Remotely: public per-category RSS feeds
  - web3.career: public HTML listing table (static, no login wall)
  - LinkedIn: public job-search page (server-renders results without login;
    the *apply* step needs your own login, same as always)

For each listing found within MAX_AGE_DAYS: fetch/derive the full
description, score it against candidate_profile.json, and - if it clears
MIN_SCORE - tailor and compile a resume for it. Everything lands in
jobs/jobs.db (jobs_db.py) and jobs/outputs/*.pdf.

Usage:
    export GOOGLE_API_KEY=...
    python3 discover_jobs.py
"""

import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent / "jobs"))
from jobs_db import JobsDB  # noqa: E402
from match_score import score as score_job  # noqa: E402
from tailor_cv import tailor as tailor_resume  # noqa: E402
from ats_boards import discover_all_ats_boards  # noqa: E402
from generate_qa import generate_qa_for_job, write_qa_markdown  # noqa: E402
from career_ops_bridge import discover_via_career_ops  # noqa: E402
from freehire_bridge import discover_via_freehire  # noqa: E402
from cover_letter import write_cover_letter  # noqa: E402
from posting_guard import guard as guard_posting  # noqa: E402

MAX_AGE_DAYS = 30
MIN_SCORE = 0.80
PROFILE_PATH = Path(__file__).parent.parent / "profile" / "candidate_profile.json"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Fallback keywords, used only if profile/candidate_profile.json doesn't
# exist yet (before setup_profile.py has run) or has no target_roles.
_DEFAULT_ROLE_KEYWORDS = {
    "DevOps": ["devops", "site reliability", "platform engineer", "infrastructure"],
    "SRE": ["site reliability", "sre "],
    "Blockchain": ["blockchain", "web3", "solidity", "smart contract", "protocol engineer"],
    "AI": ["machine learning", "ai engineer", "llm", "agentic", "artificial intelligence"],
    "Cryptography": ["cryptography", "zero-knowledge", "zk-snark", "applied crypto"],
}
_DEFAULT_TARGET_ROLES = [
    "DevOps Engineer", "Site Reliability Engineer", "Platform Engineer",
    "Blockchain Engineer", "AI Engineer", "Cryptography Engineer",
]
_SENIORITY_PREFIXES = ("senior ", "staff ", "principal ", "lead ", "junior ", "sr ", "jr ", "ii ", "iii ")


def _load_target_roles() -> list[str]:
    """Reads preferences.target_roles from the user's own profile (set by
    setup_profile.py from their actual resume) - this is what makes role
    classification/search work for any candidate, not just the original
    DevOps/Blockchain/AI/Cryptography search this repo was built for."""
    try:
        profile = json.loads(PROFILE_PATH.read_text())
        roles = profile.get("preferences", {}).get("target_roles") or []
        if roles:
            return roles
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _DEFAULT_TARGET_ROLES


def _role_keyword_variants(role: str) -> list[str]:
    role_lower = role.lower().strip()
    variants = {role_lower}
    for prefix in _SENIORITY_PREFIXES:
        if role_lower.startswith(prefix):
            variants.add(role_lower[len(prefix):])
    return list(variants)


TARGET_ROLES = _load_target_roles()
ROLE_KEYWORDS = (
    _DEFAULT_ROLE_KEYWORDS if TARGET_ROLES == _DEFAULT_TARGET_ROLES
    else {role: _role_keyword_variants(role) for role in TARGET_ROLES}
)


def _fetch(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _classify_role(title: str) -> str | None:
    title_lower = title.lower()
    for category, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return category
    return None


def _within_age(posted: datetime | None) -> bool:
    if posted is None:
        return True  # unknown date - don't exclude, just can't confirm freshness
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted) <= timedelta(days=MAX_AGE_DAYS)


def discover_remoteok() -> list[dict]:
    listings = []
    try:
        raw = _fetch("https://remoteok.com/api")
        data = json.loads(raw)
    except Exception as exc:
        print(f"  RemoteOK: fetch failed ({exc})", file=sys.stderr)
        return listings

    for item in data:
        if "position" not in item:
            continue  # first element is API metadata, not a job
        title = item.get("position", "")
        category = _classify_role(title + " " + " ".join(item.get("tags", [])))
        if not category:
            continue

        posted = None
        if item.get("date"):
            try:
                posted = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
            except ValueError:
                pass
        if not _within_age(posted):
            continue

        listings.append({
            "portal": "RemoteOK",
            "title": title,
            "company": item.get("company", ""),
            "role_category": category,
            "location": item.get("location", "Remote"),
            "apply_url": item.get("url", ""),
            "posted_date": posted.date().isoformat() if posted else None,
            "description": item.get("description", ""),
        })

    return listings


def discover_wwr() -> list[dict]:
    listings = []
    categories = ["remote-devops-sysadmin-jobs", "remote-programming-jobs"]

    for category in categories:
        try:
            raw = _fetch(f"https://weworkremotely.com/categories/{category}.rss")
            root = ET.fromstring(raw)
        except Exception as exc:
            print(f"  WeWorkRemotely ({category}): fetch failed ({exc})", file=sys.stderr)
            continue

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            role_category = _classify_role(title)
            if not role_category:
                continue

            pub_date_raw = item.findtext("pubDate")
            posted = None
            if pub_date_raw:
                try:
                    from email.utils import parsedate_to_datetime
                    posted = parsedate_to_datetime(pub_date_raw)
                except Exception:
                    pass
            if not _within_age(posted):
                continue

            company = title.split(":")[0].strip() if ":" in title else ""
            description_html = item.findtext("description") or ""
            description_text = BeautifulSoup(description_html, "html.parser").get_text(
                separator="\n", strip=True
            )

            listings.append({
                "portal": "WeWorkRemotely",
                "title": title,
                "company": company,
                "role_category": role_category,
                "location": (item.findtext("region") or "Remote"),
                "apply_url": item.findtext("link") or "",
                "posted_date": posted.date().isoformat() if posted else None,
                "description": description_text,
            })

    return listings


def discover_web3career() -> list[dict]:
    listings = []
    try:
        html = _fetch("https://web3.career/")
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        print(f"  web3.career: fetch failed ({exc})", file=sys.stderr)
        return listings

    for row in soup.select("tr.table_row"):
        title_el = row.select_one("h2, .job-title, td a")
        title = title_el.get_text(strip=True) if title_el else row.get_text(strip=True)[:80]
        role_category = _classify_role(title)
        if not role_category:
            continue

        link_el = row.select_one("a[href]")
        apply_url = f"https://web3.career{link_el['href']}" if link_el and link_el["href"].startswith("/") else (
            link_el["href"] if link_el else ""
        )
        if not apply_url:
            continue

        # web3.career shows relative age ("2d", "5h") in the row, not an
        # absolute date - treat anything with "d"/"h"/"m"(inute) as fresh,
        # and be conservative (exclude) on anything explicitly older.
        row_text = row.get_text(" ", strip=True)
        age_match = re.search(r"\b(\d+)\s*([dhm])\b", row_text)
        posted_date = None
        if age_match:
            n, unit = int(age_match.group(1)), age_match.group(2)
            if unit == "d" and n > MAX_AGE_DAYS:
                continue
            posted_date = (datetime.now(timezone.utc) - timedelta(
                days=n if unit == "d" else 0
            )).date().isoformat()

        company_el = row.select_one(".job-company, .company")
        listings.append({
            "portal": "web3.career",
            "title": title,
            "company": company_el.get_text(strip=True) if company_el else "",
            "role_category": role_category,
            "location": "Remote",
            "apply_url": apply_url,
            "posted_date": posted_date,
            "description": row_text,
        })

    return listings


def discover_remotive() -> list[dict]:
    """Remotive's public API (confirmed via its own embedded terms) permits
    personal, non-redistributed use with reasonable call volume (they ask
    for max ~4 calls/day) and a link back to the original posting - both
    satisfied here: this is a one-off personal discovery run, not a
    service republishing their listings, and apply_url always points at
    remotive.com's own posting page."""
    listings = []
    for category in ("software-dev", "devops"):
        try:
            data = json.loads(_fetch(f"https://remotive.com/api/remote-jobs?category={category}"))
        except Exception as exc:
            print(f"  Remotive ({category}): fetch failed ({exc})", file=sys.stderr)
            continue

        for job in data.get("jobs", []):
            title = job.get("title", "")
            role_category = _classify_role(title + " " + " ".join(job.get("tags", [])))
            if not role_category:
                continue

            posted = None
            if job.get("publication_date"):
                try:
                    posted = datetime.fromisoformat(job["publication_date"])
                except ValueError:
                    pass
            if not _within_age(posted):
                continue

            description = BeautifulSoup(job.get("description", ""), "html.parser").get_text(
                separator="\n", strip=True
            )
            listings.append({
                "portal": "Remotive",
                "title": title,
                "company": job.get("company_name", ""),
                "role_category": role_category,
                "location": job.get("candidate_required_location", "Remote"),
                "apply_url": job.get("url", ""),
                "posted_date": posted.date().isoformat() if posted else None,
                "description": description,
            })

    return listings


def discover_wellfound() -> list[dict]:
    """wellfound.com/jobs server-renders its listing feed (confirmed by
    direct fetch - real titles/companies/locations present with no JS
    execution). Each job link's grandparent container holds
    [title, company, '•', location, '•', salary?, relative-age] as its
    text nodes, in that fixed order - verified by sampling actual cards
    before relying on it."""
    listings = []
    try:
        html = _fetch("https://wellfound.com/jobs")
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        print(f"  Wellfound: fetch failed ({exc})", file=sys.stderr)
        return listings

    seen_urls = set()
    for link in soup.select("a[href^='/jobs/']"):
        href = link["href"]
        if not re.match(r"^/jobs/\d+-", href) or href in seen_urls:
            continue
        seen_urls.add(href)

        container = link.parent.parent if link.parent and link.parent.parent else link.parent
        strs = list(container.stripped_strings) if container else []
        if len(strs) < 4:
            continue
        title, company, location = strs[0], strs[1], strs[3]
        age_text = strs[-1]

        role_category = _classify_role(title)
        if not role_category:
            continue

        posted_date = None
        age_match = re.match(r"(\d+)d\b", age_text)
        if age_match:
            days = int(age_match.group(1))
            if days > MAX_AGE_DAYS:
                continue
            posted_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        elif age_text.lower() in ("today", "yesterday"):
            posted_date = datetime.now(timezone.utc).date().isoformat()
        elif re.search(r"mo\b|month", age_text.lower()):
            continue  # month-scale age - stale, skip without guessing an exact day

        listings.append({
            "portal": "Wellfound",
            "title": title,
            "company": company,
            "role_category": role_category,
            "location": location,
            "apply_url": "https://wellfound.com" + href,
            "posted_date": posted_date,
            "description": "",
        })

    return listings


BUILTIN_HOSTS = [
    "builtin.com", "www.builtinboston.com", "www.builtinchicago.org", "www.builtinaustin.com",
]


def discover_builtin(paths: list[str], hosts: list[str] = ("builtin.com",)) -> list[dict]:
    """builtin.com's job-category pages (e.g. /jobs/dev-ops) server-render
    real listings. Each job link's great-grandparent container holds
    [company, title, relative-age, ...] as text nodes, in that fixed
    order - verified by sampling actual cards before relying on it.
    builtin.com's city-specific sister sites (builtinboston.com etc, from
    URL.txt) share the exact same markup - confirmed by direct fetch."""
    listings = []
    for host in hosts:
        for path in paths:
            try:
                html = _fetch(f"https://{host}{path}")
                soup = BeautifulSoup(html, "html.parser")
            except Exception as exc:
                print(f"  BuiltIn ({host}{path}): fetch failed ({exc})", file=sys.stderr)
                continue

            seen_urls = set()
            for link in soup.select("a[href^='/job/']"):
                href = link["href"]
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                container = link
                for _ in range(4):
                    if container.parent is None:
                        break
                    container = container.parent
                strs = list(container.stripped_strings)
                if len(strs) < 3:
                    continue
                company, title, age_text = strs[0], strs[1], strs[2]

                role_category = _classify_role(title)
                if not role_category:
                    continue

                posted_date = None
                age_lower = age_text.lower()
                day_match = re.search(r"(\d+)\s*day", age_lower)
                if "minute" in age_lower or "hour" in age_lower or "today" in age_lower:
                    posted_date = datetime.now(timezone.utc).date().isoformat()
                elif day_match:
                    days = int(day_match.group(1))
                    if days > MAX_AGE_DAYS:
                        continue
                    posted_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
                elif "month" in age_lower or "year" in age_lower:
                    continue

                listings.append({
                    "portal": f"BuiltIn ({host})" if host != "builtin.com" else "BuiltIn",
                    "title": title,
                    "company": company,
                    "role_category": role_category,
                    "location": "",
                    "apply_url": f"https://{host}" + href,
                    "posted_date": posted_date,
                    "description": "",
                })

            time.sleep(1)

    return listings


def discover_justjoinit(categories: list[str]) -> list[dict]:
    """justjoin.it (a major European/Polish tech job board) server-renders
    its category pages (e.g. /job-offers/all-locations/devops) with real
    `/job-offer/<company-slug>-<role>-<city>-<category>` links carrying
    visible title text - confirmed by direct fetch. No confirmed per-listing
    date on these pages, so posted_date is left None (not excluded, per
    _within_age's convention) and freshness is left to match_score's own
    judgment plus this run's discovery-time recency."""
    listings = []
    for category in categories:
        try:
            html = _fetch(f"https://justjoin.it/job-offers/all-locations/{category}")
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"  JustJoinIT ({category}): fetch failed ({exc})", file=sys.stderr)
            continue

        seen_urls = set()
        for link in soup.select("a[href*='/job-offer/']"):
            title = link.get_text(strip=True)
            if not title:
                continue
            href = link["href"].split("?")[0]
            apply_url = href if href.startswith("http") else f"https://justjoin.it{href}"
            if apply_url in seen_urls:
                continue
            seen_urls.add(apply_url)

            role_category = _classify_role(title)
            if not role_category:
                continue

            listings.append({
                "portal": "JustJoinIT",
                "title": title,
                "company": "",
                "role_category": role_category,
                "location": "",
                "apply_url": apply_url,
                "posted_date": None,
                "description": "",
            })

        time.sleep(1)

    return listings


def discover_gitjobs() -> list[dict]:
    """gitjobs.dev (CNCF/Linux Foundation's open-source job board, also
    reachable at jobs.cncf.io which redirects here) server-renders its job
    cards as `button[data-job-id]` elements with data-job-title/
    data-job-company attributes right on the tag - no JS execution needed
    (confirmed by direct curl, correcting an earlier, wrong assumption
    that this site was JS-only). The full description lives behind its
    htmx partial endpoint (/section/jobs/{id}), fetched per-listing below
    only for candidates that pass role classification."""
    listings = []
    offset = 0
    while offset < 40:  # 2 pages is enough to cover this board's real volume
        try:
            html = _fetch(f"https://gitjobs.dev/?offset={offset}" if offset else "https://gitjobs.dev/")
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"  GitJobs (offset={offset}): fetch failed ({exc})", file=sys.stderr)
            break

        cards = soup.select("button[data-job-id]")
        if not cards:
            break

        for card in cards:
            title = card.get("data-job-title", "")
            role_category = _classify_role(title)
            if not role_category:
                continue

            job_id = card["data-job-id"]
            date_divs = card.select("div.uppercase")
            date_text = date_divs[1].get_text(strip=True) if len(date_divs) > 1 else ""

            posted_date = None
            try:
                parsed = datetime.strptime(f"{date_text} {datetime.now().year}", "%b %d %Y")
                if parsed > datetime.now() + timedelta(days=1):
                    parsed = parsed.replace(year=parsed.year - 1)
                posted_date = parsed.date().isoformat()
                if not _within_age(parsed.replace(tzinfo=timezone.utc)):
                    continue
            except ValueError:
                pass

            listings.append({
                "portal": "GitJobs",
                "title": title,
                "company": card.get("data-job-company", ""),
                "role_category": role_category,
                "location": "",
                "apply_url": f"https://gitjobs.dev/section/jobs/{job_id}",
                "posted_date": posted_date,
                "description": "",  # fetched per-listing below, only for those we score
            })

        offset += 20
        time.sleep(1)

    return listings


def discover_dice(queries: list[str]) -> list[dict]:
    """Dice.com's search results are server-rendered (confirmed via direct
    fetch - no login wall, no JS execution needed), unlike most other
    generic boards in URL.txt. Each job card's text nodes come in a fixed
    order: title, company, location, a bullet, then a relative age string
    ("22d ago" / "Yesterday" / "Today") - verified by sampling several
    cards' raw text before relying on it."""
    listings = []
    for q in queries:
        try:
            html = _fetch(
                f"https://www.dice.com/jobs?q={q.replace(' ', '%20')}&filters.postedDate=THIRTY"
            )
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"  Dice ({q}): fetch failed ({exc})", file=sys.stderr)
            continue

        for card in soup.select("div[data-testid=job-card]"):
            strs = list(card.stripped_strings)
            if len(strs) < 5:
                continue
            title, company, location = strs[0], strs[1], strs[2]
            age_text = strs[4]

            role_category = _classify_role(title)
            if not role_category:
                continue

            link_el = card.select_one("a[data-testid=job-search-job-card-link]")
            if not link_el or not link_el.get("href"):
                continue
            apply_url = "https://www.dice.com" + link_el["href"]

            posted_date = None
            age_match = re.match(r"(\d+)d ago", age_text)
            if age_match:
                days = int(age_match.group(1))
                if days > MAX_AGE_DAYS:
                    continue
                posted_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            elif age_text in ("Today", "Yesterday"):
                posted_date = datetime.now(timezone.utc).date().isoformat()
            # anything else (e.g. "30+ days ago") - be conservative and skip
            elif "day" in age_text.lower():
                continue

            listings.append({
                "portal": "Dice",
                "title": title,
                "company": company,
                "role_category": role_category,
                "location": location,
                "apply_url": apply_url,
                "posted_date": posted_date,
                "description": "",  # fetched per-listing below, only for those we score
            })

        time.sleep(1)

    return listings


# LinkedIn's guest search takes free-text query strings, so the user's own
# target_roles (from their resume, via setup_profile.py) work directly -
# same source as ROLE_KEYWORDS/TARGET_ROLES above.
LINKEDIN_KEYWORDS = TARGET_ROLES


def discover_linkedin(keywords: list[str]) -> list[dict]:
    """LinkedIn's public/guest search page server-renders up to ~60 results
    for a single query, but silently redirects any start>0 (pagination)
    request back to start=0 without a logged-in session (confirmed via a
    303 Location header pointing at start=0) - so pagination past the
    first page isn't available here without crossing into logged-in
    automation, which this project deliberately doesn't do (see README).
    The only legitimate way to surface more of LinkedIn's real volume is
    more distinct keyword queries, each returning its own top ~60 - hence
    the long LINKEDIN_KEYWORDS list above instead of a handful of broad
    terms."""
    listings = []
    for kw in keywords:
        try:
            html = _fetch(
                f"https://www.linkedin.com/jobs/search/?keywords={kw.replace(' ', '%20')}&f_TPR=r2592000"
            )
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"  LinkedIn ({kw}): fetch failed ({exc})", file=sys.stderr)
            continue

        for card in soup.select(".base-card"):
            title_el = card.select_one(".base-search-card__title")
            company_el = card.select_one(".base-search-card__subtitle")
            location_el = card.select_one(".job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")

            title = title_el.get_text(strip=True) if title_el else ""
            role_category = _classify_role(title)
            if not role_category or not link_el:
                continue

            listings.append({
                "portal": "LinkedIn",
                "title": title,
                "company": company_el.get_text(strip=True) if company_el else "",
                "role_category": role_category,
                "location": location_el.get_text(strip=True) if location_el else "Remote",
                "apply_url": link_el["href"].split("?")[0],
                "posted_date": None,  # LinkedIn's f_TPR=r2592000 already filters to last 30 days
                "description": "",  # fetched separately per-listing below, only for those we score
            })

        time.sleep(1)  # be polite between search queries against the same host

    return listings


def main():
    profile = json.loads(PROFILE_PATH.read_text())
    db = JobsDB()

    print("Discovering...")
    all_listings = []
    all_listings += discover_remoteok()
    print(f"  RemoteOK: {len(all_listings)} so far")
    all_listings += discover_wwr()
    print(f"  + WeWorkRemotely: {len(all_listings)} so far")
    all_listings += discover_web3career()
    print(f"  + web3.career: {len(all_listings)} so far")
    all_listings += discover_dice(TARGET_ROLES)  # free-text search, same source as LINKEDIN_KEYWORDS
    print(f"  + Dice: {len(all_listings)} so far")
    all_listings += discover_remotive()
    print(f"  + Remotive: {len(all_listings)} so far")
    all_listings += discover_wellfound()
    print(f"  + Wellfound: {len(all_listings)} so far")
    # BuiltIn/JustJoinIT category paths below are each site's own fixed
    # taxonomy slugs (a small finite set, e.g. BuiltIn's "/jobs/dev-ops"),
    # not free-text search - unlike TARGET_ROLES-driven sources above/below,
    # these two still default to this repo's original DevOps/AI/Blockchain
    # categories regardless of your profile. Edit the lists below directly
    # if your target roles fall outside those categories; every other
    # source (ATS boards, career-ops, freehire, Dice, LinkedIn, GitJobs) is
    # already profile-driven via ROLE_KEYWORDS/TARGET_ROLES.
    all_listings += discover_builtin(
        ["/jobs/dev-ops", "/jobs/blockchain-crypto", "/jobs/artificial-intelligence"],
        hosts=BUILTIN_HOSTS,
    )
    print(f"  + BuiltIn: {len(all_listings)} so far")
    all_listings += discover_gitjobs()
    print(f"  + GitJobs: {len(all_listings)} so far")
    all_listings += discover_justjoinit(["devops", "ai", "security", "go"])
    print(f"  + JustJoinIT: {len(all_listings)} so far")
    all_listings += discover_linkedin(LINKEDIN_KEYWORDS)
    print(f"  + LinkedIn: {len(all_listings)} so far")
    all_listings += discover_all_ats_boards(_classify_role, MAX_AGE_DAYS)
    print(f"  + ATS boards (Greenhouse/Lever/Ashby): {len(all_listings)} total")
    all_listings += discover_via_career_ops(_classify_role, MAX_AGE_DAYS)
    print(f"  + career-ops reverse ATS scan (Greenhouse/Lever/Ashby/Workday/iCIMS/BambooHR/Paylocity/join.com directories): {len(all_listings)} total")
    all_listings += discover_via_freehire(_classify_role, MAX_AGE_DAYS, queries=[r.lower() for r in TARGET_ROLES])
    print(f"  + freehire.me aggregator (~50 ATS platforms, many countries): {len(all_listings)} total")

    print(f"\nScoring {len(all_listings)} listings (min score {MIN_SCORE})...")
    qualifying = 0

    already_scored = 0
    for listing in all_listings:
        # Re-running discovery (e.g. after an interrupted run, or a quota
        # reset) shouldn't re-spend LLM calls on a listing already scored
        # today - same apply_url, already has a score, discovered recently.
        # A listing that previously failed to score (no match_score) is NOT
        # skipped - it gets a real retry.
        existing = db.get_by_apply_url(listing["apply_url"])
        if existing and existing["match_score"] is not None:
            already_scored += 1
            continue

        description = listing.get("description") or listing["title"]

        # LinkedIn/Dice/Wellfound/BuiltIn/GitJobs listings didn't carry a
        # description from the search page - fetch the individual posting
        # for those before scoring.
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
                    # Dice embeds the full posting as schema.org JobPosting
                    # JSON-LD - more reliable than scraping the rendered DOM,
                    # and gives us an exact datePosted too.
                    ld_el = soup.select_one("[data-testid=jobDetailStructuredData]")
                    if ld_el and ld_el.string:
                        ld_data = json.loads(ld_el.string)
                        raw_desc = ld_data.get("description", "")
                        description = BeautifulSoup(raw_desc, "html.parser").get_text(separator="\n", strip=True)
                elif listing["portal"] == "GitJobs":
                    # apply_url already points at the htmx partial that
                    # holds the full posting - just strip boilerplate.
                    for tag in soup(["script", "style"]):
                        tag.decompose()
                    description = soup.get_text(separator="\n", strip=True)
                else:
                    # Wellfound/BuiltIn - no confirmed dedicated description
                    # selector; fall back to full-page text (good enough for
                    # scoring/tailoring, same approach used in backfill_tailor.py).
                    for tag in soup(["script", "style", "nav", "footer", "header"]):
                        tag.decompose()
                    description = soup.get_text(separator="\n", strip=True)[:8000]
            except Exception:
                pass

        # Integrity gate: strip hidden HTML/invisible-Unicode/instruction-shaped
        # content from third-party posting text before it reaches any LLM
        # prompt (scoring, tailoring, cover letter, Q&A all build on this
        # cleaned description). See scripts/posting_guard.py's docstring.
        try:
            description, guard_findings = guard_posting(description, "auto")
            if guard_findings["verdict"].startswith("⚠"):
                print(f"  ⚠ posting_guard flagged {listing['title']} @ {listing['company']}: {guard_findings['verdict']}", file=sys.stderr)
        except Exception:
            pass  # guard failures shouldn't block discovery - worst case, unscrubbed text proceeds as before this gate existed

        try:
            result = score_job(description, profile)
        except Exception as exc:
            print(f"  Skipping (scoring failed): {listing['title']} - {exc}", file=sys.stderr)
            continue

        job_id = db.add_or_update(
            portal=listing["portal"],
            title=listing["title"],
            company=listing["company"],
            role_category=listing["role_category"],
            location=listing["location"],
            apply_url=listing["apply_url"],
            posted_date=listing["posted_date"],
            match_score=result["score"],
            match_reasoning=result["reasoning"],
            status="scored",
        )

        if result["score"] >= MIN_SCORE:
            qualifying += 1
            print(f"  ✓ {result['score']:.2f} {listing['title']} @ {listing['company']} ({listing['portal']})")
            try:
                pdf_path = tailor_resume(listing["company"] or listing["portal"], listing["title"], description)
                db.add_or_update(
                    portal=listing["portal"], title=listing["title"], company=listing["company"],
                    role_category=listing["role_category"], location=listing["location"],
                    apply_url=listing["apply_url"], posted_date=listing["posted_date"],
                    match_score=result["score"], match_reasoning=result["reasoning"],
                    tailored_resume_path=str(pdf_path), status="tailored",
                )
                try:
                    qa_pairs = generate_qa_for_job(description)
                    qa_path = write_qa_markdown(listing["company"] or listing["portal"], listing["title"], qa_pairs)
                    db.replace_questions(job_id, qa_pairs)
                    print(f"    Q&A ({len(qa_pairs)}): {qa_path}")
                except Exception as exc:
                    print(f"    (Q&A generation failed: {exc})", file=sys.stderr)
                try:
                    cl_path = write_cover_letter(listing["company"] or listing["portal"], listing["title"], description)
                    db.add_or_update(
                        portal=listing["portal"], title=listing["title"], company=listing["company"],
                        role_category=listing["role_category"], location=listing["location"],
                        apply_url=listing["apply_url"], posted_date=listing["posted_date"],
                        match_score=result["score"], match_reasoning=result["reasoning"],
                        tailored_resume_path=str(pdf_path), cover_letter_path=str(cl_path), status="tailored",
                    )
                    print(f"    Cover letter: {cl_path}")
                except Exception as exc:
                    print(f"    (cover letter generation failed: {exc})", file=sys.stderr)
            except Exception as exc:
                print(f"    (tailoring failed: {exc})", file=sys.stderr)

    print(f"\nDone. {qualifying} qualifying jobs (>= {MIN_SCORE}) out of {len(all_listings)} scored "
          f"({already_scored} skipped - already scored in a prior run).")
    print(json.dumps(db.summary(), indent=2))


if __name__ == "__main__":
    main()
