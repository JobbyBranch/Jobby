#!/usr/bin/env python3
"""
JobRadar scraper
────────────────
Scans the career pages in sources.yaml, detects NEW IT vacancies by
diffing against state/seen.json, and writes:

  output/new_jobs_YYYY-MM-DD.json   -> only today's new vacancies
  output/latest.json                -> the FULL current vacancy list
                                       (this is what the dashboard loads)

Runs every weekday at 08:00 Europe/Brussels (see README for scheduling).
Safe to re-run: already-seen vacancies are never reported as new twice.

JavaScript-rendered career sites (cvw.io, Cornerstone/csod, Oracle Cloud)
are fetched with Playwright headless Chromium automatically. Plain sites
that unexpectedly return zero jobs also get one rendered retry.

Optional environment variables:
  ANTHROPIC_API_KEY  -> Claude-based tech-stack + experience extraction
  SLACK_WEBHOOK_URL  -> post new vacancies to Slack
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
OUTPUT_DIR = ROOT / "output"
TZ = ZoneInfo("Europe/Brussels")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 JobRadar/1.0",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
}

# Domains known to render their vacancies with JavaScript -> Playwright
JS_RENDERED_DOMAINS = ("cvw.io", "csod.com", "oraclecloud.com")

# Titles must match at least one INCLUDE term...
# NOTE: the bare word "engineer" is deliberately NOT in this list — only
# qualified IT-engineer titles count. Mechanical/electrical/process engineers
# are excluded via NON_IT_KEYWORDS below.
IT_KEYWORDS = [
    "developer", "ontwikkelaar", "programmeur", "software",
    "devops", "cloud", "ict", "it", "it-", "network",
    "netwerk", "cyber", "security", "analist", "analyst", "architect",
    "sap", "erp", "crm", "frontend", "front-end", "backend", "back-end",
    "fullstack", "full-stack", "full stack", "java", "python", ".net",
    "php", "scrum master", "product owner", "tester", "qa", "test engineer",
    "database", "data engineer", "data scientist", "data analist",
    "data analyst", "machine learning", "ai engineer", "genai",
    "generative ai", "llm", "infrastructure", "infrastructuur",
    "helpdesk", "servicedesk", "support engineer", "applicatiebeheer",
    "application manager", "functioneel analist", "functional analyst",
    "business intelligence", "power bi", "informatica", "webmaster",
    "integration", "integratie", "automation", "automatisering",
    "system engineer", "systeembeheer", "system administrator",
    "platform engineer", "cloud engineer", "sre", "site reliability",
    "ml engineer", "security engineer", "network engineer",
]

# ...and must NOT match any EXCLUDE term. Exclusions win: this keeps out
# marketing/sales/HR/finance roles whose titles happen to contain an IT word
# ("Digital Marketing Manager", "Sales Engineer", "Data Entry Clerk", ...).
NON_IT_KEYWORDS = [
    "marketing", "sales", "verkoop", "verkoper", "commercieel", "commercial",
    "account manager", "accountmanager", "business developer",
    "business development", "hr ", " hr", "human resources", "recruiter",
    "recruitment", "talent", "payroll", "finance", "financieel", "financial",
    "accountant", "boekhoud", "accounting", "controller", "audit", "fiscaal",
    "tax ", "legal", "jurist", "advocaat", "lawyer", "communicat",
    "public relations", "pr officer", "office manager", "management assistant",
    "administratief", "administrative", "receptionist", "onthaal",
    "customer service", "customer care", "klantendienst", "logistiek",
    "logistics", "warehouse", "magazijn", "chauffeur", "driver", "operator",
    "productie", "production", "technieker hvac", "elektricien", "electricien",
    "lasser", "welder", "monteur", "mecanicien", "onderhoudstechnieker",
    "maintenance technician", "facility", "cleaning", "schoonma", "catering",
    "verpleeg", "nurse", "zorgkundige", "arts ", "dokter", "kinesist",
    "data entry", "content", "copywriter", "designer grafisch",
    "graphic designer", "social media", "e-commerce manager", "category",
    "buyer", "aankoper", "purchas", "procurement", "quality manager",
    "safety", "preventie", "milieu", "environment", "teamleider productie",
    # non-IT engineering disciplines (we are an IT consultancy)
    "mechanical", "mechanisch", "mechatronic", "hardware", "electronic",
    "elektronica", "electrical", "elektrisch", "elektrotechn",
    "process engineer", "project engineer", "field service",
    "technical service engineer", "r&d engineer", "design engineer",
    "structural", "civil engineer", "hvac", "thermal", "optical",
    "rf engineer", "quality engineer", "validation engineer",
    "manufacturing engineer", "industrial engineer", "industrialisation",
    "industrialization", "calculation engineer", "commissioning",
    "piping", "verification engineer",
    "systems integration engineer aerospace", "avionic", "embedded hardware",
]

TECH_TERMS = [
    "java", "spring", "python", "django", "flask", "c#", ".net", "php",
    "laravel", "javascript", "typescript", "react", "angular", "vue",
    "node.js", "node", "next.js", "kubernetes", "docker", "terraform",
    "aws", "azure", "gcp", "sql", "postgresql", "mysql", "oracle",
    "mongodb", "kafka", "rabbitmq", "jenkins", "gitlab", "ci/cd", "linux",
    "sap", "salesforce", "power bi", "tableau", "airflow", "spark",
    "graphql", "rest", "microservices", "scrum", "agile",
]

# A link only counts as a vacancy if its URL plausibly belongs to the
# career section: same company site AND (under the career page's path, OR a
# job-word in the URL, OR a dedicated jobs host / known recruitment platform).
JOB_PATH_TOKENS = re.compile(
    r"(job|vacature|vacanc|career|carriere|position|werkenbij|werken-bij|"
    r"sollicit|emploi|join-us|joinus|opportunit)", re.I)

KNOWN_ATS_DOMAINS = (
    "cvw.io", "csod.com", "oraclecloud.com", "recruitee.com",
    "teamtailor.com", "workable.com", "jobtoolz.com", "greenhouse.io",
    "lever.co", "smartrecruiters.com", "hr-technologies.com",
)

DEDICATED_JOB_SUBDOMAINS = ("jobs", "careers", "career", "werkenbij",
                            "workat", "vacatures", "talent")


def _registrable(host: str) -> str:
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def plausible_job_url(href: str, base_url: str) -> bool:
    h, b = urlparse(href), urlparse(base_url)
    same_site = _registrable(h.netloc) == _registrable(b.netloc)
    on_ats = any(h.netloc.endswith(d) for d in KNOWN_ATS_DOMAINS)
    if not (same_site or on_ats):
        return False
    if on_ats:
        return True
    if h.netloc.split(".")[0] in DEDICATED_JOB_SUBDOMAINS:
        return True
    base_path = b.path.rstrip("/")
    if base_path and h.path.rstrip("/").startswith(base_path):
        return True
    if not base_path:  # source is the root of a dedicated jobs site
        return True
    return bool(JOB_PATH_TOKENS.search(h.path))


LINK_BLACKLIST = re.compile(
    r"(privacy|cookie|login|facebook|linkedin|twitter|instagram|mailto:|tel:|"
    r"#$|javascript:|\.pdf$|\.jpg$|\.png$|about|contact|blog|news|nieuws)",
    re.I,
)

# ── Playwright (lazy, shared browser) ─────────────────────────────────
_browser = None
_playwright = None


def _get_browser():
    global _browser, _playwright
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    return _browser


def close_browser():
    global _browser, _playwright
    if _browser:
        _browser.close()
        _playwright.stop()
        _browser = _playwright = None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_rendered(url: str) -> str | None:
    """Fetch a page with headless Chromium so client-side JS runs."""
    try:
        browser = _get_browser()
        page = browser.new_page(user_agent=HEADERS["User-Agent"],
                                locale="nl-BE")
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)  # give lazy widgets a moment
        html = page.content()
        page.close()
        return html
    except Exception as e:
        print(f"  ! rendered fetch failed: {e}")
        return None


def needs_rendering(url: str) -> bool:
    host = urlparse(url).netloc
    return any(host.endswith(d) or d in host for d in JS_RENDERED_DOMAINS)


# ── Core helpers ──────────────────────────────────────────────────────
def guard_schedule() -> None:
    """When SCHEDULE_GUARD=1, only proceed at 08:00 local on weekdays.

    GitHub Actions cron is UTC: we trigger at 06:00 and 07:00 UTC and this
    guard keeps only the run that is 08:00 in Brussels, so DST is handled.
    """
    if os.environ.get("SCHEDULE_GUARD") != "1":
        return
    now = datetime.now(TZ)
    if now.weekday() >= 5:
        print(f"[guard] {now:%a %H:%M} — weekend, skipping.")
        sys.exit(0)
    if now.hour != 8:
        print(f"[guard] {now:%a %H:%M} — not the 08:00 Brussels run, skipping.")
        sys.exit(0)


def load_sources() -> list[dict]:
    with open(ROOT / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def load_state() -> dict:
    """seen.json maps vacancy URL -> job dict.

    Migrates the old format (url -> "YYYY-MM-DD" string) transparently.
    """
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    migrated = {}
    for url, val in raw.items():
        if isinstance(val, str):
            migrated[url] = {"url": url, "first_seen": val,
                             "title": "", "company": "", "stack": []}
        else:
            migrated[url] = val
    return migrated


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def fetch(url: str, force_render: bool = False) -> str | None:
    if force_render or needs_rendering(url):
        if playwright_available():
            print("  (rendering with headless browser)")
            return fetch_rendered(url)
        print("  ! page needs JS rendering but Playwright is not installed "
              "(pip install playwright && playwright install chromium)")
        if force_render:
            return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"  ! fetch failed: {e}")
        return None


def _match_any(text: str, keywords) -> bool:
    """Whole-word match for short keywords (<=4 chars, e.g. erp/hr/it/bi),
    substring match for longer ones. Prevents 'beheERPortaal'-style hits."""
    t = text.lower()
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        if len(k) <= 4:
            if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t):
                return True
        elif k in t:
            return True
    return False


def looks_like_it_job(title: str) -> bool:
    if _match_any(title, NON_IT_KEYWORDS):
        return False
    return _match_any(title, IT_KEYWORDS)


_classify_cache = {}


def classify_with_claude(title: str) -> bool | None:
    """Optional second opinion: is this title an IT job? None = unavailable.

    Only used when ANTHROPIC_API_KEY is set. Cached per title.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    if title in _classify_cache:
        return _classify_cache[title]
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 5,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Is this an IT job (software, data engineering, infrastructure, "
                        "cybersecurity, IT support, IT analysis/architecture)? "
                        "Marketing, sales, HR, finance, legal, logistics, healthcare "
                        "and manual/technical trades are NOT IT, even if the title "
                        "mentions digital or data. Job title (Dutch or English): "
                        f"'{title}'. Answer with exactly one word: yes or no."
                    ),
                }],
            },
            timeout=20,
        )
        r.raise_for_status()
        answer = r.json()["content"][0]["text"].strip().lower()
        result = answer.startswith("y")
        _classify_cache[title] = result
        return result
    except Exception:
        return None  # fall back to keyword decision


def extract_job_links(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen_here = [], set()
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"])
        if not title or len(title) < 6 or len(title) > 120:
            continue
        if LINK_BLACKLIST.search(href) or LINK_BLACKLIST.search(title):
            continue
        if urlparse(href).netloc == "":
            continue
        if not plausible_job_url(href, base_url):
            continue
        keyword_says_it = looks_like_it_job(title)
        ai_says_it = classify_with_claude(title)
        is_it = ai_says_it if ai_says_it is not None else keyword_says_it
        if not is_it:
            continue
        key = href.split("#")[0]
        if key in seen_here:
            continue
        seen_here.add(key)
        jobs.append({"title": title, "url": key})
    return jobs


def extract_stack_keywords(text: str) -> list[str]:
    t = f" {text.lower()} "
    found = [term for term in TECH_TERMS if f" {term} " in t or f"{term}," in t]
    return found[:8]


def enrich_with_claude(job: dict, page_text: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        job["stack"] = extract_stack_keywords(page_text or job["title"])
        return job
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Extract the tech stack and required years of experience "
                        "from this vacancy. Reply ONLY with JSON: "
                        '{"stack": ["java", ...], "experience": "5+ yrs" or null}\n\n'
                        f"Title: {job['title']}\n\nPage text:\n{page_text[:6000]}"
                    ),
                }],
            },
            timeout=40,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        data = json.loads(re.sub(r"```json|```", "", text).strip())
        job["stack"] = data.get("stack", [])[:10]
        job["experience"] = data.get("experience")
    except Exception as e:
        print(f"  ! enrichment failed ({e}), falling back to keyword scan")
        job["stack"] = extract_stack_keywords(page_text or job["title"])
    return job


def notify_slack(new_jobs: list[dict]) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook or not new_jobs:
        return
    lines = [f"*JobRadar — {len(new_jobs)} new IT vacancies found* :radar:"]
    for j in new_jobs[:20]:
        stack = ", ".join(j.get("stack", [])[:5])
        lines.append(f"• *{j['company']}* — <{j['url']}|{j['title']}>"
                     + (f" _( {stack} )_" if stack else ""))
    try:
        requests.post(webhook, json={"text": "\n".join(lines)}, timeout=15)
    except requests.RequestException as e:
        print(f"  ! slack notify failed: {e}")


def write_outputs(state: dict, new_jobs: list[dict], now: datetime) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    daily = OUTPUT_DIR / f"new_jobs_{now:%Y-%m-%d}.json"
    daily.write_text(json.dumps(new_jobs, indent=2, ensure_ascii=False),
                     encoding="utf-8")

    # latest.json: full current list, newest first — the dashboard reads this
    all_jobs = sorted(state.values(),
                      key=lambda j: j.get("first_seen", ""), reverse=True)
    (OUTPUT_DIR / "latest.json").write_text(
        json.dumps(all_jobs, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    guard_schedule()
    now = datetime.now(TZ)
    print(f"JobRadar scan — {now:%A %d %b %Y, %H:%M} (Europe/Brussels)")
    if not playwright_available():
        print("Note: Playwright not installed — JS-rendered pages "
              "(cvw.io / csod / Oracle Cloud) will be skipped.\n")

    sources = load_sources()
    state = load_state()
    new_jobs = []

    try:
        for src in sources:
            company, url = src["company"], src["url"]
            print(f"\n[{company}] {url}")
            html = fetch(url)
            if html is None:
                continue
            jobs = extract_job_links(html, url)

            # Fallback: static fetch found nothing but the page may be JS-driven
            if not jobs and not needs_rendering(url) and playwright_available():
                print("  0 links via plain fetch — retrying with rendering")
                html = fetch(url, force_render=True)
                if html:
                    jobs = extract_job_links(html, url)

            print(f"  found {len(jobs)} IT-looking job links")
            for job in jobs:
                if job["url"] in state:
                    continue
                job["company"] = company
                job["source"] = url
                job["first_seen"] = now.strftime("%Y-%m-%d")
                detail = fetch(job["url"]) if job["url"] != url else html
                page_text = BeautifulSoup(detail or "", "html.parser") \
                    .get_text(" ", strip=True)
                job = enrich_with_claude(job, page_text)
                state[job["url"]] = job
                new_jobs.append(job)
                print(f"  + NEW: {job['title']}  "
                      f"[{', '.join(job.get('stack', []))}]")
                time.sleep(1)  # be polite
            time.sleep(1.5)
    finally:
        close_browser()

    save_state(state)
    write_outputs(state, new_jobs, now)
    print(f"\nDone: {len(new_jobs)} new vacancies · "
          f"{len(state)} total in latest.json")
    notify_slack(new_jobs)


if __name__ == "__main__":
    main()
