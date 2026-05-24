"""
Job scraper for Israeli tech companies.
Runs daily via GitHub Actions and stores results in Supabase.

Strategy:
  1. Detect ATS platform (from URL or by inspecting page iframes)
  2. Use platform-specific API when available (fast + accurate + includes description)
  3. Fallback to browser HTML scraping with smart filtering
  4. Phase 2 enrichment with Claude only for jobs missing description
"""

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs

import anthropic
import httpx
from playwright.async_api import async_playwright, BrowserContext, Page
from supabase import create_client, Client

# ─── Config ──────────────────────────────────────────────────────────────────

COMPANIES_FILE = os.path.join(os.path.dirname(__file__), "companies.json")

ISRAEL_KEYWORDS = {
    "israel", "tel aviv", "tel-aviv", "herzliya", "herzeliya", "petah tikva",
    "petach tikva", "raanana", "ra'anana", "rishon", "haifa", "beer sheva",
    "be'er sheva", "netanya", "rehovot", "holon", "givatayim", "kfar saba",
    "yokneam", "yoqneam", "rosh haayin", "caesarea", "ramat gan", "modiin",
    "jerusalem", "ישראל", "תל אביב", "הרצליה", "פתח תקווה", "ראשון לציון",
    "חיפה", "באר שבע", "נתניה", "רחובות", "הולון", "כפר סבא", "ירושלים",
    "רמת גן", "יקנעם",
}

# Reject these link texts (clearly not job titles)
SKIP_LINK_WORDS = {
    "login", "sign in", "register", "home", "about", "contact", "blog",
    "privacy", "terms", "faq", "newsletter", "press", "media", "investors",
    "cookie", "sitemap", "accessibility", "עברית", "english", "follow",
    "instagram", "twitter", "linkedin", "facebook", "youtube", "apply",
    "apply now", "learn more", "read more", "see all", "view all", "load more",
    "next", "previous", "back to", "search jobs", "all jobs", "all departments",
    "view job", "see details", "more info",
}

# Reject URLs that point to apply forms / generic pages, not job descriptions
SKIP_URL_PATTERNS = [
    r"/apply($|[/?#])", r"/application", r"job_app", r"/login", r"/register",
    r"/refer", r"\.pdf($|\?)", r"mailto:", r"tel:", r"javascript:",
]

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_DETAILS_PER_RUN = 300  # Phase 2 cap per run

# Default browser headers
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


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
        # If job already comes with description, mark as fetched
        if job.get("description"):
            job["details_fetched"] = True
            job["details_fetched_at"] = now
    try:
        # Chunk to avoid payload limits
        for i in range(0, len(jobs), 100):
            supabase.table("jobs").upsert(jobs[i:i+100], on_conflict="url").execute()
        print(f"  ✓ Upserted {len(jobs)} jobs")
    except Exception as e:
        print(f"  ✗ Supabase error: {e}", file=sys.stderr)


def mark_inactive_jobs(supabase: Client, company: str, active_urls: set[str]):
    try:
        res = supabase.table("jobs").select("url").eq("company", company).eq("is_active", True).execute()
        existing = {row["url"] for row in res.data}
        stale = existing - active_urls
        if stale:
            for i in range(0, len(stale), 100):
                chunk = list(stale)[i:i+100]
                supabase.table("jobs").update({"is_active": False}).in_("url", chunk).execute()
            print(f"  → Marked {len(stale)} jobs as inactive")
    except Exception as e:
        print(f"  ✗ Could not mark inactive for {company}: {e}", file=sys.stderr)


def get_jobs_needing_details(supabase: Client) -> list[dict]:
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    if not html:
        return ""
    txt = TAG_RE.sub(" ", html)
    txt = unescape(txt)
    return WS_RE.sub(" ", txt).strip()


def is_israel_location(loc: str) -> bool:
    if not loc:
        return False
    low = loc.lower()
    return any(kw in low for kw in ISRAEL_KEYWORDS)


def clean_title(title: str, location: str = "") -> str:
    """Strip common junk that gets appended by HTML scraping."""
    if not title:
        return ""
    t = title.strip()
    # Remove trailing "Apply" / "Apply Now"
    t = re.sub(r"\s*(Apply Now|Apply|הגישו מועמדות|הגש מועמדות)\s*$", "", t, flags=re.I)
    # Remove trailing location if it matches what we know
    if location:
        loc_pat = re.escape(location.split(",")[0].strip())
        if loc_pat:
            t = re.sub(rf"\s*{loc_pat}.*$", "", t, flags=re.I)
    # Also strip well-known city names from end of title
    for city in ["Herzliya", "Tel Aviv", "Tel-Aviv", "Petah Tikva", "Ra'anana",
                 "Raanana", "Jerusalem", "Israel"]:
        t = re.sub(rf"\s*{re.escape(city)}\s*$", "", t, flags=re.I)
    return WS_RE.sub(" ", t).strip()


def url_looks_like_apply_form(url: str) -> bool:
    low = url.lower()
    return any(re.search(p, low) for p in SKIP_URL_PATTERNS)


# ─── ATS: Greenhouse (board API) ──────────────────────────────────────────────

def extract_greenhouse_slug(url: str) -> tuple[str | None, str | None]:
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=|embed/job_app\?for=)?([^/?#&]+)",
                  url)
    slug = None
    if m:
        slug = m.group(1)
        if slug in ("embed", "job-boards", "job_boards"):
            slug = None
    if not slug:
        # job-boards.greenhouse.io/{slug}
        m = re.search(r"job-boards\.greenhouse\.io/([^/?#&]+)", url)
        slug = m.group(1) if m else None
    if not slug:
        # for=xxx query param
        q = parse_qs(urlparse(url).query)
        slug = (q.get("for") or [None])[0]
    office_id = None
    m2 = re.search(r"(?:offices(?:\[\])?=|gh_office=)(\d+)", url)
    if m2:
        office_id = m2.group(1)
    return slug, office_id


async def scrape_greenhouse(http: httpx.AsyncClient, company: str, url: str,
                            slug: str | None = None, office_id: str | None = None) -> list[dict]:
    if slug is None:
        slug, office_id = extract_greenhouse_slug(url)
    if not slug:
        return []

    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        resp = await http.get(api)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  Greenhouse API error for {company}: {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        location = (job.get("location") or {}).get("name", "") or ""
        offices = job.get("offices") or []
        office_ids = {str(o.get("id", "")) for o in offices}
        office_names = " ".join((o.get("name") or "") for o in offices)
        is_il = is_israel_location(location) or is_israel_location(office_names)
        if office_id and office_id in office_ids:
            is_il = True
        if not is_il:
            continue
        content_html = unescape(job.get("content") or "")
        description = strip_html(content_html)[:5000] or None
        jobs.append({
            "company": company,
            "title": (job.get("title") or "").strip(),
            "url": job.get("absolute_url") or "",
            "location": location,
            "is_active": True,
            "description": description,
        })
    return jobs


# ─── ATS: Comeet ─────────────────────────────────────────────────────────────

async def scrape_comeet(http: httpx.AsyncClient, company: str, url: str) -> list[dict]:
    m = re.search(r"comeet\.com/jobs/([^/]+)/([^/?#]+)", url)
    if not m:
        return []
    slug, code = m.group(1), m.group(2)
    m2 = re.search(r"location=([^&]+)", url)
    loc_param = f"?location={m2.group(1)}" if m2 else ""
    api = f"https://www.comeet.com/jobs/{slug}/{code}.json{loc_param}"
    try:
        resp = await http.get(api)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  Comeet API error for {company}: {e}")
        return []

    jobs = []
    positions = data if isinstance(data, list) else data.get("positions", [])
    for p in positions:
        loc = (p.get("location") or {})
        city = loc.get("city", "") or ""
        country = loc.get("country", "") or ""
        loc_str = ", ".join([x for x in [city, country] if x])
        if not is_israel_location(loc_str):
            continue
        # Description: comeet positions can include details directly
        desc_html = p.get("details", {}).get("description") if isinstance(p.get("details"), dict) else None
        desc = strip_html(desc_html or "")[:5000] or None
        jobs.append({
            "company": company,
            "title": (p.get("name") or "").strip(),
            "url": p.get("url_active") or p.get("url") or url,
            "location": loc_str,
            "is_active": True,
            "description": desc,
        })
    return jobs


# ─── ATS: Ashby ──────────────────────────────────────────────────────────────

def extract_ashby_slug(url: str) -> str | None:
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"ashbyhq\.com/(?:embed\?org=)([^&]+)", url)
    if m:
        return m.group(1)
    return None


async def scrape_ashby(http: httpx.AsyncClient, company: str, url: str,
                       slug: str | None = None) -> list[dict]:
    if slug is None:
        slug = extract_ashby_slug(url)
    if not slug:
        return []
    api = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    try:
        resp = await http.get(api)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  Ashby API error for {company}: {e}")
        return []

    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", "") or ""
        if not is_israel_location(loc):
            # Check secondary locations
            sec_list = j.get("secondaryLocations", []) or []
            sec = " ".join(s if isinstance(s, str) else (s.get("location") or s.get("name") or "")
                           for s in sec_list)
            if not is_israel_location(sec):
                continue
        desc_html = j.get("descriptionHtml") or ""
        desc = strip_html(desc_html)[:5000] or None
        jobs.append({
            "company": company,
            "title": (j.get("title") or "").strip(),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "location": loc,
            "is_active": True,
            "description": desc,
        })
    return jobs


# ─── ATS: Lever ──────────────────────────────────────────────────────────────

def extract_lever_slug(url: str) -> str | None:
    m = re.search(r"(?:jobs\.lever\.co|lever\.co/(?:postings/)?)([^/?#]+)", url)
    if m:
        slug = m.group(1)
        if slug not in ("postings",):
            return slug
    return None


async def scrape_lever(http: httpx.AsyncClient, company: str, url: str,
                       slug: str | None = None) -> list[dict]:
    if slug is None:
        slug = extract_lever_slug(url)
    if not slug:
        return []
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = await http.get(api)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  Lever API error for {company}: {e}")
        return []

    jobs = []
    for p in data:
        loc = (p.get("categories") or {}).get("location", "") or ""
        add = p.get("additional", "") or ""
        all_locs = add if isinstance(add, str) else " ".join(str(x) for x in add)
        if not is_israel_location(loc) and not is_israel_location(all_locs):
            continue
        desc_parts = [p.get("descriptionPlain") or strip_html(p.get("description") or "")]
        for lst in p.get("lists", []) or []:
            desc_parts.append(strip_html(lst.get("content", "")))
        desc = (" ".join(x for x in desc_parts if x))[:5000] or None
        jobs.append({
            "company": company,
            "title": (p.get("text") or "").strip(),
            "url": p.get("hostedUrl") or p.get("applyUrl") or "",
            "location": loc,
            "is_active": True,
            "description": desc,
        })
    return jobs


# ─── ATS: Workable ───────────────────────────────────────────────────────────

def extract_workable_slug(url: str) -> str | None:
    m = re.search(r"(?:apply\.workable\.com|jobs\.workable\.com)/([^/?#]+)", url)
    return m.group(1) if m else None


async def scrape_workable(http: httpx.AsyncClient, company: str, url: str,
                          slug: str | None = None) -> list[dict]:
    if slug is None:
        slug = extract_workable_slug(url)
    if not slug:
        return []
    api = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    try:
        resp = await http.post(api, json={"query": "", "location": {"countryCode": "IL"}})
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  Workable API error for {company}: {e}")
        return []

    jobs = []
    for j in data.get("results", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        if not is_israel_location(loc):
            continue
        url_to = f"https://apply.workable.com/{slug}/j/{j['shortcode']}/"
        jobs.append({
            "company": company,
            "title": (j.get("title") or "").strip(),
            "url": url_to,
            "location": loc,
            "is_active": True,
            "description": None,  # Workable description requires per-job fetch
        })
    return jobs


# ─── ATS: SmartRecruiters ───────────────────────────────────────────────────

def extract_smartrecruiters_slug(url: str) -> str | None:
    m = re.search(r"smartrecruiters\.com/([^/?#]+)", url)
    if m and m.group(1) not in ("api",):
        return m.group(1)
    return None


async def scrape_smartrecruiters(http: httpx.AsyncClient, company: str, url: str,
                                 slug: str | None = None) -> list[dict]:
    if slug is None:
        slug = extract_smartrecruiters_slug(url)
    if not slug:
        return []
    api = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?country=il&limit=100"
    try:
        resp = await http.get(api)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        print(f"  SmartRecruiters API error for {company}: {e}")
        return []

    jobs = []
    for p in data.get("content", []):
        loc = (p.get("location") or {})
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        if not is_israel_location(loc_str):
            continue
        jobs.append({
            "company": company,
            "title": (p.get("name") or "").strip(),
            "url": p.get("ref") or p.get("applyUrl") or "",
            "location": loc_str,
            "is_active": True,
            "description": None,
        })
    return jobs


# ─── ATS detection in URL ────────────────────────────────────────────────────

def detect_ats_from_url(url: str) -> tuple[str | None, str | None]:
    """Returns (ats_name, slug) if URL matches a known ATS pattern."""
    if "greenhouse.io" in url or "gh_office=" in url:
        slug, _ = extract_greenhouse_slug(url)
        return ("greenhouse", slug) if slug else (None, None)
    if "comeet.com" in url:
        return ("comeet", None)
    if "ashbyhq.com" in url:
        slug = extract_ashby_slug(url)
        return ("ashby", slug) if slug else (None, None)
    if "lever.co" in url:
        slug = extract_lever_slug(url)
        return ("lever", slug) if slug else (None, None)
    if "workable.com" in url:
        slug = extract_workable_slug(url)
        return ("workable", slug) if slug else (None, None)
    if "smartrecruiters.com" in url:
        slug = extract_smartrecruiters_slug(url)
        return ("smartrecruiters", slug) if slug else (None, None)
    return (None, None)


# ─── Detect ATS by inspecting careers page ──────────────────────────────────

async def detect_ats_from_page(page: Page) -> tuple[str | None, str | None]:
    """Look for ATS iframes / API calls in the page."""
    try:
        # Check all iframes for known ATS
        iframes = await page.query_selector_all("iframe[src]")
        for iframe in iframes:
            src = await iframe.get_attribute("src") or ""
            ats, slug = detect_ats_from_url(src)
            if ats:
                return ats, slug

        # Check page HTML for embed URLs
        html = await page.content()
        for ats_check in [
            (r"(?:greenhouse\.io/embed/job_board\?for=|boards\.greenhouse\.io/|job-boards\.greenhouse\.io/)([\w\-]+)", "greenhouse"),
            (r"jobs\.ashbyhq\.com/([\w\-]+)", "ashby"),
            (r"jobs\.lever\.co/([\w\-]+)", "lever"),
            (r"apply\.workable\.com/([\w\-]+)", "workable"),
            (r"smartrecruiters\.com/([\w\-]+)", "smartrecruiters"),
            (r"comeet\.com/jobs/([\w\-]+)", "comeet"),
        ]:
            pat, name = ats_check
            m = re.search(pat, html)
            if m:
                return name, m.group(1)
    except Exception:
        pass
    return None, None


# ─── Claude-based HTML extractor (high accuracy fallback) ──────────────────

def claude_extract_jobs_from_html(company: str, page_url: str, page_text: str) -> list[dict]:
    """Send page text to Claude and ask it to extract real job postings."""
    if not os.environ.get("ANTHROPIC_API_KEY") or not page_text.strip():
        return []
    ai = anthropic.Anthropic()
    prompt = f"""You extract real job postings from a company careers page.
Return ONLY a JSON array, no explanation.

Company: {company}
Page URL: {page_url}
Page text (visible content):
\"\"\"
{page_text[:12000]}
\"\"\"

Rules:
- Include ONLY real, individual job openings (e.g. "Senior Backend Engineer", "Product Manager - Growth")
- EXCLUDE: navigation links, product names, department headers, blog posts, category filters,
  "Apply", "Sign up", login buttons, "All jobs", company info, marketing copy
- Include ONLY jobs based in ISRAEL (Tel Aviv, Herzliya, Jerusalem, Haifa, etc.). If location isn't shown but the page is clearly Israel-only, include them.
- For URL: if a job has its own page URL, include it. If relative (starts with /), keep it relative.
  If no individual URL exists, use the page URL.
- If NO real Israel jobs visible, return []
- Max 100 jobs

Return JSON array of:
{{"title": "exact job title", "url": "job URL (relative ok)", "location": "city or Israel"}}"""
    try:
        response = ai.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return []
        data = json.loads(m.group(0))
        result = []
        for j in data:
            if not isinstance(j, dict):
                continue
            url = (j.get("url") or "").strip()
            if url and not url.startswith("http"):
                url = urljoin(page_url, url)
            if not url:
                url = page_url
            if url_looks_like_apply_form(url):
                continue
            title = (j.get("title") or "").strip()
            if not title or len(title) < 3:
                continue
            result.append({
                "company": company,
                "title": title,
                "url": url,
                "location": (j.get("location") or "Israel").strip(),
                "is_active": True,
                "description": None,
            })
        return result
    except Exception as e:
        print(f"  ✗ Claude extract error: {e}")
        return []


async def get_page_visible_text(page: Page) -> str:
    try:
        html = await page.content()
        # Strip script/style tags entirely
        clean = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>",
                       " ", html, flags=re.DOTALL | re.I)
        # Also keep some link info (href + text) by replacing <a> with text + url
        clean = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{1,120})</a>',
                       r" \2 [\1] ", clean, flags=re.I)
        text = strip_html(clean)
        return text
    except Exception:
        return ""


# ─── Generic browser scraper ─────────────────────────────────────────────────

def is_likely_job_link(href: str, text: str, base_domain: str) -> bool:
    if not href or not text:
        return False
    text = text.strip()
    if len(text) < 4 or len(text) > 150:
        return False
    low = text.lower()
    if any(low == w or low.startswith(w + " ") or low.endswith(" " + w) for w in SKIP_LINK_WORDS):
        return False
    if url_looks_like_apply_form(href):
        return False
    try:
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            return False
        link_domain = parsed.netloc
        if base_domain not in link_domain and link_domain not in base_domain:
            allowed = {"greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
                       "taleo.net", "zohorecruit.com", "hibob.com", "comeet.com",
                       "workable.com", "smartrecruiters.com", "eightfold.ai",
                       "myworkdayjobs.com", "icims.com"}
            if not any(b in link_domain for b in allowed):
                return False
    except Exception:
        return False
    return True


def find_job_links_by_pattern(all_links: list[tuple[str, str]], base_domain: str) -> list[tuple[str, str]]:
    pattern_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for href, text in all_links:
        if not href:
            continue
        try:
            parsed = urlparse(href)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) < 2:
                continue
            pattern = parsed.netloc + "/" + "/".join(parts[:-1])
            pattern_groups[pattern].append((href, text))
        except Exception:
            continue

    if not pattern_groups:
        return []

    # Score patterns: prefer ones with many distinct URLs AND varied titles
    def score(items):
        urls = {u for u, _ in items}
        texts = {t for _, t in items if t}
        return len(urls) * (1 + min(len(texts), 50) / 50.0)

    best = max(pattern_groups, key=lambda k: score(pattern_groups[k]))
    candidates = [(u, t) for u, t in pattern_groups[best] if is_likely_job_link(u, t, base_domain)]
    if len(candidates) < 2:
        return []

    seen = set()
    out = []
    for href, text in candidates:
        if href not in seen:
            seen.add(href)
            out.append((href, text))
    return out


async def extract_json_ld_jobs(page: Page, company: str) -> list[dict]:
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
                    # ItemList of JobPostings
                    if item.get("@type") == "ItemList":
                        for li in item.get("itemListElement", []) or []:
                            it = li.get("item") if isinstance(li, dict) else None
                            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                                items.append(it)
                        continue
                    if item.get("@type") == "JobPosting":
                        loc_obj = item.get("jobLocation", {})
                        if isinstance(loc_obj, list):
                            loc_obj = loc_obj[0] if loc_obj else {}
                        addr = (loc_obj or {}).get("address", {}) or {}
                        location = (
                            addr.get("addressLocality") or addr.get("addressRegion")
                            or addr.get("addressCountry") or ""
                        )
                        if isinstance(location, dict):
                            location = location.get("name", "")
                        if not is_israel_location(str(location)):
                            continue
                        desc = strip_html(item.get("description", ""))[:5000]
                        jobs.append({
                            "company": company,
                            "title": (item.get("title") or "").strip(),
                            "url": item.get("url") or page.url,
                            "location": str(location),
                            "is_active": True,
                            "description": desc or None,
                        })
            except (json.JSONDecodeError, AttributeError):
                continue
    except Exception as e:
        print(f"  JSON-LD extraction error: {e}")
    return jobs


async def scroll_and_load_more(page: Page, max_iterations: int = 6):
    """Scroll to bottom + try clicking 'Load more' buttons."""
    last_count = -1
    for _ in range(max_iterations):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
            # Try common "Load more" buttons
            for sel in [
                'button:has-text("Load more")', 'button:has-text("Show more")',
                'button:has-text("View more")', 'button:has-text("המשך")',
                'a:has-text("Load more")', 'a:has-text("Show more")',
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click(timeout=2000)
                        await page.wait_for_timeout(1200)
                except Exception:
                    pass
            count = await page.evaluate("document.querySelectorAll('a').length")
            if count == last_count:
                break
            last_count = count
        except Exception:
            break


async def scrape_with_browser(context: BrowserContext, company: str, career_url: str,
                              http: httpx.AsyncClient) -> list[dict]:
    page = await context.new_page()
    jobs: list[dict] = []
    try:
        try:
            await page.goto(career_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"  ✗ Could not load {career_url}: {e}")
            return []
        await page.wait_for_timeout(2500)

        # 1) Detect embedded ATS (iframe / inline script) → route to dedicated API
        ats, slug = await detect_ats_from_page(page)
        if ats:
            print(f"  → detected embedded {ats} ({slug})")
            if ats == "greenhouse":
                return await scrape_greenhouse(http, company, career_url, slug=slug)
            if ats == "ashby":
                return await scrape_ashby(http, company, career_url, slug=slug)
            if ats == "lever":
                return await scrape_lever(http, company, career_url, slug=slug)
            if ats == "workable":
                return await scrape_workable(http, company, career_url, slug=slug)
            if ats == "smartrecruiters":
                return await scrape_smartrecruiters(http, company, career_url, slug=slug)
            if ats == "comeet":
                # Slug-only detection isn't enough for comeet (needs code)
                pass

        # 2) JSON-LD structured data
        jobs = await extract_json_ld_jobs(page, company)
        if jobs:
            print(f"  → {len(jobs)} jobs via JSON-LD")
            return jobs

        # 3) Try to expand the page (lazy loading)
        await scroll_and_load_more(page)

        # 4) Claude extraction from visible page text (most accurate fallback)
        page_text = await get_page_visible_text(page)
        if page_text and len(page_text) > 300:
            jobs = await asyncio.to_thread(
                claude_extract_jobs_from_html, company, career_url, page_text
            )
            if jobs:
                print(f"  → {len(jobs)} jobs via Claude HTML extraction")
                return jobs

        # 5) Last-resort: link pattern heuristic
        base_domain = urlparse(career_url).netloc.replace("www.", "")
        link_els = await page.query_selector_all("a[href]")
        all_links = []
        for el in link_els:
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

        print(f"  → {len(job_links)} jobs via link pattern (fallback)")

        for href, title in job_links[:150]:
            jobs.append({
                "company": company,
                "title": clean_title(title),
                "url": href,
                "location": "Israel",
                "is_active": True,
                "description": None,
            })
    except Exception as e:
        print(f"  ✗ Browser error for {company}: {e}")
    finally:
        await page.close()
    return jobs


# ─── Phase 2: detail extraction for jobs missing description ────────────────

async def fetch_job_description(context: BrowserContext, url: str) -> str:
    page = await context.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return ""
        await page.wait_for_timeout(1500)
        # First try JSON-LD on the job page itself
        try:
            scripts = await page.query_selector_all('script[type="application/ld+json"]')
            for s in scripts:
                content = await s.text_content() or ""
                try:
                    data = json.loads(content)
                    items = data if isinstance(data, list) else [data]
                    for it in items:
                        if it.get("@type") == "JobPosting" and it.get("description"):
                            return strip_html(it["description"])[:5000]
                except Exception:
                    continue
        except Exception:
            pass
        # Then try main-content selectors
        for sel in ["main", "article", '[class*="job-description"]',
                    '[class*="description"]', '[id*="description"]',
                    '[class*="posting"]', "body"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and len(text) > 100:
                        return text[:5000]
            except Exception:
                continue
    finally:
        await page.close()
    return ""


def extract_details_with_claude(description: str, title: str) -> dict:
    if not description or not description.strip():
        return {}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"description": description[:300]}

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
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        print(f"    ✗ Claude error: {e}")
        return {}


async def enrich_jobs(supabase: Client, context: BrowserContext):
    jobs = get_jobs_needing_details(supabase)
    if not jobs:
        print("\nNo jobs need detail extraction.")
        return
    print(f"\n── Phase 2: Enriching {len(jobs)} jobs ──")

    sem = asyncio.Semaphore(4)  # parallel fetches

    async def process(i: int, job: dict):
        async with sem:
            print(f"  [{i}/{len(jobs)}] {job['company']} — {job['title'][:50]}")
            desc = await fetch_job_description(context, job["url"])
            if not desc:
                save_job_details(supabase, job["id"], {"description": None})
                return
            details = extract_details_with_claude(desc, job["title"])
            save_job_details(supabase, job["id"], details)

    await asyncio.gather(*(process(i, j) for i, j in enumerate(jobs, 1)))
    print(f"  ✓ Enrichment complete")


# ─── Per-company dispatch ────────────────────────────────────────────────────

async def scrape_company(context: BrowserContext, http: httpx.AsyncClient, company: dict) -> list[dict]:
    name = company["name"]
    url = company["url"]

    ats, slug = detect_ats_from_url(url)
    print(f"\n[{name}] ({ats or 'browser'})")

    try:
        if ats == "greenhouse":
            jobs = await scrape_greenhouse(http, name, url, slug=slug,
                                            office_id=extract_greenhouse_slug(url)[1])
        elif ats == "comeet":
            jobs = await scrape_comeet(http, name, url)
        elif ats == "ashby":
            jobs = await scrape_ashby(http, name, url, slug=slug)
        elif ats == "lever":
            jobs = await scrape_lever(http, name, url, slug=slug)
        elif ats == "workable":
            jobs = await scrape_workable(http, name, url, slug=slug)
        elif ats == "smartrecruiters":
            jobs = await scrape_smartrecruiters(http, name, url, slug=slug)
        else:
            jobs = await scrape_with_browser(context, name, url, http)

        # Filter + clean
        cleaned = []
        for j in jobs:
            if not j.get("title") or not j.get("url"):
                continue
            j["title"] = clean_title(j["title"], j.get("location", ""))
            if not j["title"]:
                continue
            if url_looks_like_apply_form(j["url"]):
                continue
            cleaned.append(j)
        print(f"  Total: {len(cleaned)} jobs")
        return cleaned
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        return []


# ─── Concurrency-controlled main ─────────────────────────────────────────────

async def main():
    with open(COMPANIES_FILE) as f:
        companies = json.load(f)

    supabase = get_supabase()
    total_jobs = 0

    print(f"── Phase 1: Scraping {len(companies)} companies ──\n")

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20,
        headers={"User-Agent": UA, "Accept": "application/json,text/html,*/*"},
    ) as http:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=UA, locale="en-US",
                viewport={"width": 1280, "height": 800},
            )

            # Process companies with bounded concurrency, write INCREMENTALLY
            sem = asyncio.Semaphore(3)
            PER_COMPANY_TIMEOUT = 90  # seconds — kill anything stuck longer

            async def handle(c):
                async with sem:
                    try:
                        jobs = await asyncio.wait_for(
                            scrape_company(context, http, c),
                            timeout=PER_COMPANY_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        print(f"  ⚠ {c['name']} timed out after {PER_COMPANY_TIMEOUT}s")
                        jobs = []
                    except Exception as e:
                        print(f"  ✗ {c['name']} crashed: {e}")
                        jobs = []
                    # Write to DB immediately so progress is durable
                    if jobs:
                        upsert_jobs(supabase, jobs)
                        mark_inactive_jobs(supabase, c["name"], {j["url"] for j in jobs})
                    return c["name"], len(jobs)

            results = await asyncio.gather(*(handle(c) for c in companies),
                                           return_exceptions=True)
            for r in results:
                if isinstance(r, tuple):
                    total_jobs += r[1]

            print(f"\n✅ Phase 1 done. Total jobs: {total_jobs}")

            # Phase 2: enrich any jobs still missing description
            await enrich_jobs(supabase, context)

            await context.close()
            await browser.close()

    print("\n✅ All done.")


if __name__ == "__main__":
    asyncio.run(main())
