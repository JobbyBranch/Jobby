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

import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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
JS_RENDERED_DOMAINS = ("cvw.io", "csod.com", "oraclecloud.com" "reynaers.com", "amptec.be", "dpgmediagroup.com", "vandewiele.com",)

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
    "piping", "verification engineer", "cnc", "nc-programmeur",
    "nc - programmeur", "verspaner", "draaier-frezer",
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

# Playwright's sync API is thread-bound: every call must happen on the thread
# that started it. All rendering is therefore routed through this single-worker
# executor — one thread owns the browser for the whole run.
_RENDER_POOL = None

def _render_pool():
    global _RENDER_POOL
    if _RENDER_POOL is None:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _RENDER_POOL = _TPE(max_workers=1, thread_name_prefix="render")
    return _RENDER_POOL


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
    """Thread-safe wrapper: executes the real render on the browser thread."""
    return _render_pool().submit(_fetch_rendered_impl, url).result()


def _fetch_rendered_impl(url: str) -> str | None:
    """Fetch a page with headless Chromium so client-side JS runs."""
    try:
        browser = _get_browser()
        page = browser.new_page(user_agent=HEADERS["User-Agent"],
                                locale="nl-BE")
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_timeout(3500)  # let client-side widgets settle
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


NAV_JUNK = ("arrow_forward_ios", "arrow_forward", "read more", "lees meer",
            "meer informatie", "apply now", "solliciteer nu")

def clean_title(title: str) -> str:
    t = title.strip()
    for junk in NAV_JUNK:
        t = re.sub(re.escape(junk), "", t, flags=re.I).strip(" -·|")
    return re.sub(r"\s{2,}", " ", t).strip()


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
                        "NOT IT: marketing, sales, HR, finance, legal, logistics, "
                        "healthcare, manual/technical trades, procurement/purchasing "
                        "(aankoper, buyer — even of IT), quality/mechanical/electrical/"
                        "process/field-service engineering, and bare category words "
                        "that are not a concrete vacancy (like 'Engineering', 'Jobs', "
                        "'Techniek'). CNC/NC programming (machine operating) and pure "
                        "PLC/machine/robot automation without software development are "
                        "NOT IT. If the title clearly states a work location outside "
                        "Belgium (e.g. a Dutch, German or French city), answer no. "
                        "Job title (Dutch or English): "
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
        # hard exclusions (mechanical/quality/process engineers, sales, HR...)
        # always win — the AI classifier may NOT override them
        if _match_any(title, NON_IT_KEYWORDS):
            continue
        keyword_says_it = _match_any(title, IT_KEYWORDS)
        ai_says_it = classify_with_claude(title)
        is_it = ai_says_it if ai_says_it is not None else keyword_says_it
        if not is_it:
            continue
        key = href.split("#")[0]
        if key in seen_here:
            continue
        seen_here.add(key)
        title = clean_title(title)
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



def anthropic_call(payload: dict, timeout: int = 60) -> dict | None:
    """POST to the Anthropic API with retries on overload (529/429/5xx)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=timeout,
            )
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(6 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(6 * (attempt + 1))
    raise RuntimeError("Anthropic API overloaded after 3 attempts")


def anthropic_text(resp: dict | None) -> str:
    if not isinstance(resp, dict):
        return ""
    content = resp.get("content") or []
    if not content or not isinstance(content[0], dict):
        return ""
    return content[0].get("text") or ""


def parse_first_json(text: str):
    """Parse the FIRST JSON object in the text, ignoring anything after it."""
    idx = text.find("{")
    if idx < 0:
        raise ValueError(f"no JSON in model reply: {text[:120]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text[idx:])
    return obj


# ── AI MATCHING (Level 3) ─────────────────────────────────────────────
def load_candidates() -> list[dict]:
    """Fetch the published candidates sheet (CSV). Returns [] when not configured.

    Expected columns: Name, Role, Years, Skills, Profile (career digest).
    Row index (0-based, data rows only) is the candidate's stable reference —
    names are never written into match output for privacy.
    """
    url = os.environ.get("CANDIDATES_CSV_URL")
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
        if len(rows) < 2:
            return []
        header = [h.strip().lower() for h in rows[0]]
        def col(*names):
            for n in names:
                for i, h in enumerate(header):
                    if n in h:
                        return i
            return -1
        iN, iR = col("name", "naam"), col("role", "functie")
        iY, iS = col("year", "ervaring"), col("skill")
        iP = col("profile", "digest", "history")
        cands = []
        for idx, row in enumerate(rows[1:]):
            if iN < 0 or iN >= len(row) or not row[iN].strip():
                continue
            get = lambda i: row[i].strip() if 0 <= i < len(row) else ""
            cands.append({
                "row": idx,
                "name": get(iN),
                "role": get(iR),
                "years": int(re.sub(r"\D", "", get(iY)) or 0),
                "skills": [x.strip().lower() for x in re.split(r"[,;]+", get(iS)) if x.strip()],
                "profile": get(iP),
            })
        print(f"[matching] loaded {len(cands)} candidates"
              f" ({sum(1 for c in cands if c['profile'])} with career digest)")
        return cands
    except Exception as e:
        print(f"[matching] could not load candidates: {e}")
        return []


def prefilter_candidates(job: dict, candidates: list[dict], top: int = 10) -> list[dict]:
    """Cheap keyword pass: keep only plausibly relevant candidates for the AI."""
    stack = set(x.lower() for x in job.get("stack", []))
    title = job.get("title", "").lower()
    title_tokens = set(w for w in re.findall(r"[a-z\+#\.]+", title) if len(w) > 3)
    scored = []
    for c in candidates:
        skills = set(c["skills"])
        role_tokens = set(w for w in re.findall(r"[a-z\+#\.]+", c["role"].lower()) if len(w) > 3)
        overlap = len(stack & skills)
        title_skill = sum(1 for sk in skills if len(sk) > 2 and sk in title)
        role_overlap = len(title_tokens & role_tokens)
        scored.append((overlap * 2 + title_skill + role_overlap * 2, c))
    scored.sort(key=lambda x: -x[0])
    picked = [c for sc, c in scored[:top] if sc > 0]
    if picked:
        return picked
    # no signal at all: send a DIVERSE cross-section of the bench (varied roles),
    # not simply the first ten sheet rows
    step = max(1, len(candidates) // top)
    return candidates[::step][:top]


def _match_check(c: dict) -> str:
    return (c["name"][:1].lower() or "?") + str(c["years"])


def ai_match_job(job: dict, page_text: str, candidates: list[dict]) -> dict:
    """Ask Claude to judge the shortlist against the full vacancy text."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not candidates:
        return job
    shortlist = prefilter_candidates(job, candidates)
    if not shortlist:
        return job
    lines = []
    for c in shortlist:
        hist = c["profile"][:2400] if c["profile"] else "(no career digest — judge on role/skills only)"
        lines.append(f"ROW={c['row']} | {c['role']} | {c['years']} yrs | "
                     f"skills: {', '.join(c['skills'][:14])} | history: {hist}")
    prompt = (
        "You are a senior IT recruiter at a Belgian consultancy. Pick the 3 best "
        "candidates for this vacancy.\n"
        "Score = the likelihood a client would interview this candidate for this role:\n"
        "  85-100: submit immediately — stack and seniority fit, concrete matching history\n"
        "  70-84: strong fit — right stack, minor gaps (missing nice-to-haves, adjacent domain)\n"
        "  55-69: good fit worth pitching — core skills present, real but coachable gaps\n"
        "  35-54: partial fit — some overlap, would need selling\n"
        "  <35: weak — wrong profile\n"
        "Calibration rules:\n"
        "- Concrete past work in the history outweighs keyword overlap.\n"
        "- Years-of-experience requirements are indicative, NOT hard bars: a candidate "
        "within ~70% of the asked years with an exact stack match still scores in the "
        "strong range (e.g. 5 yrs for a '7 yrs' vacancy with matching stack: 70+, not 40s). "
        "Only penalize heavily when the seniority gap is large (junior vs architect).\n"
        "- 'Senior' in a title is about capability signals in the history (ownership, "
        "architecture, mentoring), not just the year count.\n"
        "- Be honest — if the fit is genuinely weak, score low.\n"
        "Refer to candidates ONLY by the exact ROW= number shown (these are "
        "sheet row numbers, NOT positions 1-10 in this list).\n"
        'Reply ONLY with JSON: {"matches": [{"row": <int>, "score": <0-100>, '
        '"reason": "<one concrete sentence, max 20 words, citing their relevant history>"}]} '
        "with exactly 3 entries, best first.\n\n"
        f"VACANCY: {job['title']} at {job['company']}\n"
        f"FULL TEXT:\n{page_text[:6000]}\n\n"
        f"CANDIDATES:\n" + "\n".join(lines)
    )
    try:
        resp = anthropic_call({"model": "claude-sonnet-4-6", "max_tokens": 800,
                               "messages": [{"role": "user", "content": prompt}]})
        text = anthropic_text(resp)
        data = parse_first_json(text)
        # STRICT: only shortlist rows are valid answers. If the model answered
        # with 1-based shortlist positions instead of sheet rows, remap them.
        shortlist_rows = {c["row"] for c in shortlist}
        raw = data.get("matches", [])[:3]
        raw_rows = [int(m.get("row", -1)) for m in raw]
        if raw_rows and not all(rr in shortlist_rows for rr in raw_rows) \
                and all(1 <= rr <= len(shortlist) for rr in raw_rows):
            for m, rr in zip(raw, raw_rows):
                m["row"] = shortlist[rr - 1]["row"]
            print("    (remapped positional rows to sheet rows)")
        by_row = {c["row"]: c for c in shortlist}
        out, used_rows = [], set()
        for m in raw:
            row = int(m.get("row", -1))
            if row not in by_row or row in used_rows:
                continue
            used_rows.add(row)
            reason = str(m.get("reason", ""))[:300]
            for c in candidates:  # privacy scrub: no names in public output
                if c["name"] and c["name"] in reason:
                    reason = reason.replace(c["name"], f"row {c['row']}")
                first = c["name"].split()[0] if c["name"] else ""
                if len(first) > 2 and first in reason:
                    reason = reason.replace(first, f"row {c['row']}")
            out.append({"row": row, "score": max(0, min(100, int(m.get("score", 0)))),
                        "reason": reason, "check": _match_check(by_row[row])})
        if out:
            job["ai_matches"] = out
            print(f"    ai-matched: rows {[m['row'] for m in out]}"
                  f" ({[m['score'] for m in out]}%)")
    except Exception as e:
        print(f"    ! ai matching failed: {e}")
    return job


def notify_slack(new_jobs: list[dict], candidates: list[dict] | None = None) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook or not new_jobs:
        return
    by_row = {c["row"]: c for c in (candidates or [])}
    lines = [f"*JobRadar — {len(new_jobs)} new IT vacancies found* :radar:"]
    for j in new_jobs[:20]:
        stack = ", ".join(j.get("stack", [])[:5])
        line = (f"• *{j['company']}* — <{j['url']}|{j['title']}>"
                + (f" _( {stack} )_" if stack else ""))
        am = j.get("ai_matches") or []
        if am and am[0].get("row") in by_row:
            top = by_row[am[0]["row"]]
            line += f"\n    ↳ best match: *{top['name']}* ({am[0].get('score', 0)}%)"
        lines.append(line)
    if len(new_jobs) > 20:
        lines.append(f"_…and {len(new_jobs) - 20} more in the dashboard_")
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
    candidates = load_candidates()
    new_jobs = []

    workers = int(os.environ.get("SCAN_WORKERS", "8"))
    state_lock = threading.Lock()          # guards state + new_jobs
    render_sem = threading.Semaphore(2)    # max concurrent headless browsers
    ai_sem = threading.Semaphore(3)        # max concurrent Anthropic calls

    def scan_source(src):
        company, url = src["company"], src["url"]
        lines = [f"[{company}] {url}"]
        found_new = []
        try:
            html = fetch(url)
            if html is None:
                lines.append("  fetch failed")
                return lines, found_new
            jobs = extract_job_links(html, url)
            if not jobs and not needs_rendering(url) and playwright_available():
                lines.append("  0 links via plain fetch — retrying with rendering")
                with render_sem:
                    html = fetch(url, force_render=True)
                if html:
                    jobs = extract_job_links(html, url)
            lines.append(f"  found {len(jobs)} IT-looking job links")
            for job in jobs:
                with state_lock:
                    if job["url"] in state:
                        continue
                job["company"] = company
                job["source"] = url
                job["first_seen"] = now.strftime("%Y-%m-%d")
                detail = fetch(job["url"]) if job["url"] != url else html
                page_text = BeautifulSoup(detail or "", "html.parser") \
                    .get_text(" ", strip=True)
                with ai_sem:
                    job = enrich_with_claude(job, page_text)
                    job = ai_match_job(job, page_text, candidates)
                with state_lock:
                    if job["url"] in state:      # re-check after slow AI step
                        continue
                    state[job["url"]] = job
                    new_jobs.append(job)
                found_new.append(job)
                lines.append(f"  + NEW: {job['title']}  "
                             f"[{', '.join(job.get('stack', []))}]")
                time.sleep(0.6)   # politeness within one site
        except Exception as e:
            lines.append(f"  ! source failed: {e}")
        return lines, found_new

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(scan_source, src): src for src in sources}
            done_count = 0
            for fut in as_completed(futures):
                lines, _ = fut.result()
                done_count += 1
                print("\n" + "\n".join(lines) +
                      f"\n  ({done_count}/{len(sources)} sources done)")
    finally:
        try:
            _render_pool().submit(close_browser).result(timeout=30)
            _render_pool().shutdown(wait=False)
        except Exception as e:
            print(f"(browser cleanup issue ignored: {e})")

    save_state(state)
    write_outputs(state, new_jobs, now)
    print(f"\nDone: {len(new_jobs)} new vacancies · "
          f"{len(state)} total in latest.json")
    notify_slack(new_jobs, candidates)


if __name__ == "__main__":
    main()
