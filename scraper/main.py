"""
Job scraper for Israeli tech companies.
Runs daily via GitHub Actions and stores results in Supabase.

Two-phase approach:
  Phase 1 (fast): Scrape job listings → get title + URL + location
  Phase 2 (smart): For NEW jobs only → fetch description + extract skills via Claude
"""

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import anthropic
import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from supabase import create_client, Client

# ─── Config ──────────────────────────────────────────────────────────────────

COMPANIES_FILE = os.path.join(os.path.dirname(__file__), "companies.json")

ISRAEL_KEYWORDS = {
    "israel", "tel aviv", "herzliya", "petah tikva", "raanana", "ra'anana",
    "rishon", "haifa", "beer sheva", "netanya", "rehovot", "holon",
    "givatayim", "kfar saba", "yokneam", "rosh haayin", "caesarea",
    "ישראל", "תל אביב", "הרצליה", "פתח תקווה", "ראשון לציון",
    "חיפה", "באר שבע", "נתניה", "רחובות", "הולון", "כפר סבא"
}

SKIP_LINK_WORDS = {
    "login", "sign in", "register", "home", "about", "contact", "blog",
    "privacy", "terms", "faq", "newsletter", "press", "media", "investors",
    "cookie", "sitemap", "accessibility", "עברית", "english", "follow",
    "instagram", "twitter", "linkedin", "facebook", "youtube"
}

# Claude model for extraction (fast + cheap)
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
# Max jobs to enrich with details per run (to stay within rate limits)
MAX_DETAILS_PER_RUN = 200


# ─── Supabase ─────────────────────────────────────────────────────────────────

def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


def upsert_jobs(supabase: Client, jobs: list[dict]):
    if not jobs:
        return
    now = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        job["last_seen"] = now
    try:
        supabase.table("jobs").upsert(jobs, on_conflict="url").execute()
        print(f"  ✓ Upserted {len(jobs)} jobs")
    except Exception as e:
        print(f"  ✗ Supabase error: {e}", file=sys.stderr)


def mark_inactive_jobs(supabase: Client, company: str, active_urls: set[str]):
    try:
        res = supabase.table("jobs").select("url").eq("company", company).eq("is_active", True).execute()
        existing_urls = {row["url"] for row in res.data}
        stale = existing_urls - active_urls
        if stale:
            supabase.table("jobs").update({"is_active": False}).in_("url", list(stale)).execute()
            print(f"  → Marked {len(stale)} jobs as inactive")
    except Exception as e:
        print(f"  ✗ Could not mark inactive for {company}: {e}", file=sys.stderr)


def get_jobs_needing_details(supabase: Client) -> list[dict]:
    """Return jobs that don't have details fetched yet."""
    try:
        res = (
            supabase.table("jobs")
            .select("id, url, title, company")
            .eq("is_active", True)
            .eq("details_fetched", False)
            .limit(MAX_DETAILS_PER_RUN)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"✗ Could not fetch jobs needing details: {e}", file=sys.stderr)
        return []


def save_job_details(supabase: Client, job_id: str, details: dict):
    try:
        supabase.table("jobs").update({
            **details,
            "details_fetched": True,
            "details_fetched_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
    except Exception as e:
        print(f"  ✗ Could not save details for job {job_id}: {e}", file=sys.stderr)


# ─── ATS: Greenhouse ──────────────────────────────────────────────────────────

def extract_greenhouse_slug(url: str) -> tuple[str | None, str | None]:
    m = re.search(r"greenhouse\.io/([^/?#&]+)", url)
    slug = m.group(1) if m else None
    m2 = re.search(r"(?:offices\[\]=|gh_office=)(\d+)", url)
    office_id = m2.group(1) if m2 else None
    return slug, office_id


async def scrape_greenhouse(company_name: str, url: str) -> list[dict]:
    slug, office_id = extract_greenhouse_slug(url)
    if not slug:
        return []

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(api_url)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as e:
        print(f"  Greenhouse API error for {company_name}: {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        location = job.get("location", {}).get("name", "") or ""
        offices = job.get("offices", []) or []
        office_ids = {str(o.get("id", "")) for o in offices}
        loc_lower = location.lower()
        is_israel = any(kw in loc_lower for kw in ISRAEL_KEYWORDS)
        if office_id and office_id in office_ids:
            is_israel = True
        if not is_israel and office_id:
            continue
        if not is_israel and not office_id:
            continue

        jobs.append({
            "company": company_name,
            "title": job.get("title", "").strip(),
            "url": job.get("absolute_url", ""),
            "location": location,
            "is_active": True,
        })

    return jobs


# ─── ATS: Comeet ─────────────────────────────────────────────────────────────

async def scrape_comeet(company_name: str, url: str) -> list[dict]:
    m = re.search(r"comeet\.com/jobs/([^/]+)/([^/?#]+)", url)
    if not m:
        return []
    slug, code = m.group(1), m.group(2)
    m2 = re.search(r"location=([^&]+)", url)
    loc_param = f"?location={m2.group(1)}" if m2 else ""
    api_url = f"https://www.comeet.com/jobs/{slug}/{code}.json{loc_param}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(api_url)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as e:
        print(f"  Comeet API error for {company_name}: {e}")
        return []

    jobs = []
    for position in data if isinstance(data, list) else data.get("positions", []):
        loc = position.get("location", {}).get("city", "") or ""
        country = position.get("location", {}).get("country", "") or ""
        loc_lower = (loc + " " + country).lower()
        if not any(kw in loc_lower for kw in ISRAEL_KEYWORDS):
            continue
        jobs.append({
            "company": company_name,
            "title": position.get("name", "").strip(),
            "url": position.get("url_active", url),
            "location": f"{loc}, {country}".strip(", "),
            "is_active": True,
        })

    return jobs


# ─── Browser scraper (Playwright) ────────────────────────────────────────────

def is_likely_job_link(href: str, text: str, base_domain: str) -> bool:
    if not href or not text:
        return False
    text = text.strip()
    if len(text) < 4 or len(text) > 150:
        return False
    if any(w in text.lower() for w in SKIP_LINK_WORDS):
        return False
    try:
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            return False
        link_domain = parsed.netloc
        if base_domain not in link_domain and link_domain not in base_domain:
            allowed_boards = {"greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
                              "taleo.net", "zohorecruit.com", "hibob.com", "comeet.com"}
            if not any(board in link_domain for board in allowed_boards):
                return False
    except Exception:
        return False
    return True


def find_job_links_by_pattern(all_links: list[tuple[str, str]], base_domain: str) -> list[tuple[str, str]]:
    """Detect job listings by finding the most repeated URL pattern."""
    pattern_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for href, text in all_links:
        if not href:
            continue
        try:
            parsed = urlparse(href)
            path_parts = [p for p in parsed.path.split("/") if p]
            if len(path_parts) < 2:
                continue
            pattern = parsed.netloc + "/" + "/".join(path_parts[:-1])
            pattern_groups[pattern].append((href, text))
        except Exception:
            continue

    if not pattern_groups:
        return []

    best_pattern = max(pattern_groups, key=lambda k: len(set(u for u, _ in pattern_groups[k])))
    candidates = [(u, t) for u, t in pattern_groups[best_pattern] if is_likely_job_link(u, t, base_domain)]

    if len(candidates) < 2:
        return []

    seen = set()
    result = []
    for href, text in candidates:
        if href not in seen:
            seen.add(href)
            result.append((href, text))
    return result


async def extract_json_ld_jobs(page: Page, company_name: str) -> list[dict]:
    jobs = []
    try:
        scripts = await page.query_selector_all('script[type="application/ld+json"]')
        for script in scripts:
            content = await script.text_content()
            if not content:
                continue
            try:
                data = json.loads(content)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        loc_obj = item.get("jobLocation", {})
                        if isinstance(loc_obj, list):
                            loc_obj = loc_obj[0] if loc_obj else {}
                        addr = loc_obj.get("address", {})
                        location = addr.get("addressLocality", "") or addr.get("addressRegion", "") or ""
                        jobs.append({
                            "company": company_name,
                            "title": item.get("title", "").strip(),
                            "url": item.get("url", page.url),
                            "location": location,
                            "is_active": True,
                        })
            except (json.JSONDecodeError, AttributeError):
                continue
    except Exception as e:
        print(f"  JSON-LD extraction error: {e}")
    return jobs


async def scrape_with_browser(context: BrowserContext, company_name: str, career_url: str) -> list[dict]:
    page = await context.new_page()
    jobs = []

    try:
        await page.goto(career_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)

        jobs = await extract_json_ld_jobs(page, company_name)
        if jobs:
            print(f"  → {len(jobs)} jobs via JSON-LD")
            return jobs

        base_domain = urlparse(career_url).netloc
        link_elements = await page.query_selector_all("a[href]")
        all_links = []
        for el in link_elements:
            try:
                href = await el.get_attribute("href")
                text = (await el.text_content() or "").strip()
                if href and not href.startswith("#"):
                    if href.startswith("/"):
                        href = urljoin(career_url, href)
                    all_links.append((href, text))
            except Exception:
                continue

        job_links = find_job_links_by_pattern(all_links, base_domain)

        if not job_links:
            print(f"  → 0 jobs found")
            return []

        print(f"  → {len(job_links)} jobs via link pattern")

        for href, title in job_links[:100]:
            jobs.append({
                "company": company_name,
                "title": title,
                "url": href,
                "location": "Israel",
                "is_active": True,
            })

    except Exception as e:
        print(f"  ✗ Browser error for {company_name}: {e}")
    finally:
        await page.close()

    return jobs


# ─── Phase 2: Job detail extraction ──────────────────────────────────────────

async def fetch_job_description(context: BrowserContext, url: str) -> str:
    """Visit a single job page and return the main text content."""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Try to get the main content area
        for selector in ["main", "article", '[class*="job-description"]',
                          '[class*="description"]', '[id*="description"]', "body"]:
            el = await page.query_selector(selector)
            if el:
                text = await el.inner_text()
                if text and len(text) > 100:
                    return text[:5000]  # Cap at 5000 chars for Claude
    except Exception as e:
        print(f"    ✗ Could not fetch {url}: {e}")
    finally:
        await page.close()
    return ""


def extract_details_with_claude(description: str, title: str) -> dict:
    """Use Claude Haiku to extract structured data from job description."""
    if not description.strip():
        return {}

    ai = anthropic.Anthropic()
    prompt = f"""Extract structured data from this job posting. Return ONLY valid JSON, no explanation.

Job title: {title}
Job description:
{description}

Return this exact JSON structure:
{{
  "description": "1-3 sentence summary of the role",
  "years_experience": "number or range, e.g. '3', '3-5', '5+', or null if not mentioned",
  "seniority": "one of: Junior, Mid, Senior, Lead, Manager, Director, or null",
  "employment_type": "one of: Full-time, Part-time, Contract, or null",
  "hard_skills": ["array", "of", "technical", "tools", "languages", "frameworks"],
  "soft_skills": ["array", "of", "interpersonal", "soft", "skills"]
}}"""

    try:
        response = ai.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Handle possible markdown code block wrapping
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        print(f"    ✗ Claude extraction error: {e}")
        return {}


async def enrich_jobs_with_details(supabase: Client, context: BrowserContext):
    """Phase 2: Fetch description + extract details for new jobs."""
    jobs = get_jobs_needing_details(supabase)
    if not jobs:
        print("\nNo new jobs need detail extraction.")
        return

    print(f"\n── Phase 2: Enriching {len(jobs)} new jobs with details ──")

    for i, job in enumerate(jobs, 1):
        print(f"  [{i}/{len(jobs)}] {job['company']} — {job['title'][:50]}")

        description_text = await fetch_job_description(context, job["url"])
        if not description_text:
            # Mark as fetched even if empty, so we don't retry forever
            save_job_details(supabase, job["id"], {"description": None})
            continue

        details = extract_details_with_claude(description_text, job["title"])
        save_job_details(supabase, job["id"], details)

        # Small delay to respect rate limits
        await asyncio.sleep(0.5)

    print(f"  ✓ Enrichment complete")


# ─── Main orchestrator ────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    if "greenhouse.io" in url or "gh_office=" in url:
        return "greenhouse"
    if "comeet.com" in url:
        return "comeet"
    return "browser"


async def scrape_company(context: BrowserContext, company: dict) -> list[dict]:
    name = company["name"]
    url = company["url"]
    ats = detect_ats(url)

    print(f"\n[{name}] ({ats})")

    try:
        if ats == "greenhouse":
            jobs = await scrape_greenhouse(name, url)
        elif ats == "comeet":
            jobs = await scrape_comeet(name, url)
        else:
            jobs = await scrape_with_browser(context, name, url)

        jobs = [j for j in jobs if j.get("title") and j.get("url")]
        print(f"  Total: {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        return []


async def main():
    with open(COMPANIES_FILE) as f:
        companies = json.load(f)

    supabase = get_supabase()
    total_jobs = 0

    print(f"── Phase 1: Scraping {len(companies)} companies ──\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="he-IL",
            viewport={"width": 1280, "height": 800},
        )

        # Phase 1: Collect job listings
        for company in companies:
            jobs = await scrape_company(context, company)
            if jobs:
                upsert_jobs(supabase, jobs)
                mark_inactive_jobs(supabase, company["name"], {j["url"] for j in jobs})
                total_jobs += len(jobs)

        print(f"\n✅ Phase 1 done. Total jobs: {total_jobs}")

        # Phase 2: Enrich new jobs with descriptions + skills
        await enrich_jobs_with_details(supabase, context)

        await context.close()
        await browser.close()

    print("\n✅ All done.")


if __name__ == "__main__":
    asyncio.run(main())
