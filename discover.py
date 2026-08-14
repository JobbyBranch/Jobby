#!/usr/bin/env python3
"""
JobRadar source discovery — v3 (crawl-first, rebrand-aware, block-aware)

What changed vs v2 (and why v2 found almost nothing on real batches):
  1. HOMEPAGE FIRST. v2 only guessed 14 fixed paths; any company whose
     vacancy page lives elsewhere was invisible. v3 fetches the homepage,
     extracts every link whose URL or anchor text smells like jobs, and
     probes those first. The fixed paths remain as fallback.
  2. REBRANDS FOLLOWED. v2 rejected every redirect to a different domain —
     which killed all multinationals (zeiss.be -> zeiss.com, covestro.be ->
     covestro.com). v3 treats the domain the ROOT redirects to as the
     company's real domain, and trusts links found on the company's own
     homepage even when they point elsewhere (career subdomains, group
     sites, ATS platforms). Blind path-guesses stay strict.
  3. BLOCKED IS ITS OWN VERDICT. Sites behind bot protection give the
     GitHub runner a 403/challenge; v2 silently counted that as
     "no-career-page". v3 reports it as "blocked" so the report shows how
     big that problem actually is.

Still deliberately requests-only (no browser): JS-only sites remain out of
reach; that's the accepted trade for speed on thousands of companies.

  - 12 companies probed in parallel
  - dead/parked domains detected with one cheap root-check, then skipped
  - hard time budget per company
  - results flushed to disk continuously — a timeout or crash loses nothing
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
import yaml

ROOT = Path(__file__).parent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 8
PER_COMPANY_BUDGET = 45   # seconds, hard cap
WORKERS = 12
MAX_HOMEPAGE_LINKS = 6    # most promising job-ish homepage links to probe

PATTERNS = [
    "https://jobs.{d}",
    "https://careers.{d}",
    "https://werkenbij.{d}",
    "https://www.{d}/jobs",
    "https://www.{d}/nl/jobs",
    "https://www.{d}/vacatures",
    "https://www.{d}/nl/vacatures",
    "https://www.{d}/careers",
    "https://www.{d}/en/careers",
    "https://www.{d}/werken-bij",
    "https://www.{d}/nl/werken-bij",
    "https://{d}/jobs",
    "https://{d}/vacatures",
    "https://{d}/careers",
]

# redirect targets that are legitimate career platforms
KNOWN_ATS = {
    "recruitee.com", "jobtoolz.com", "teamtailor.com", "workable.com",
    "greenhouse.io", "lever.co", "smartrecruiters.com", "cvwarehouse.com",
    "cvw.io", "csod.com", "oraclecloud.com", "successfactors.com",
    "successfactors.eu", "workday.com", "myworkdayjobs.com", "talentlyft.com",
    "homerun.co", "join.com", "personio.de", "personio.com", "onlyfy.jobs",
    "hr-technologies.com", "talentfinder.be", "jobsolutions.be",
}
# never accept these as a "career page" (redirect traps / socials / parking)
REJECT_HOSTS = {
    "instagram.com", "facebook.com", "linkedin.com", "youtube.com",
    "twitter.com", "x.com", "google.com", "godaddy.com", "sedoparking.com",
    "dan.com", "combell.com", "vimeo.com", "tiktok.com", "indeed.com",
    "jobat.be", "vdab.be", "glassdoor.com",
}

BLOCK_STATUS = {401, 403, 405, 406, 409, 429, 503}
BLOCK_MARKERS = ("cloudflare", "captcha", "access denied", "request blocked",
                 "bot detection", "are you a human", "attention required",
                 "ddos", "perimeterx", "imperva", "akamai")


def _reg(host):
    host = host.lower().replace("www.", "")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


JOBISH = re.compile(
    r"(vacature|vacatures|job|jobs|career|carri[eè]re|careers|solliciteer|"
    r"werken bij|werk bij|join (our|the) team|join us|open positions|"
    r"opportunit|word collega|onze mensen|team versterken)", re.I)

A_TAG = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
                   re.I | re.S)

_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "ses"):
        _local.ses = requests.Session()
        _local.ses.headers.update(HEADERS)
    return _local.ses


def load_existing_domains() -> set:
    src = ROOT / "sources.yaml"
    if not src.exists():
        return set()
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    domains = set()
    for entry in data.get("sources", []):
        host = urlparse(entry["url"]).netloc.lower().replace("www.", "")
        parts = host.split(".")
        domains.add(".".join(parts[-2:]) if len(parts) >= 2 else host)
    return domains


def load_companies() -> list[tuple[str, str]]:
    lines = []
    for fname in ("companies.txt", "companies_auto.txt"):
        p = ROOT / fname
        if p.exists():
            lines += p.read_text(encoding="utf-8").splitlines()
    out, seen = [], set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or ";" not in line:
            continue
        name, domain = [p.strip() for p in line.split(";", 1)]
        base = domain.lower().replace("www.", "")
        if base and base not in seen:
            seen.add(base)
            out.append((name, base))
    return out


def looks_blocked(status: int, text: str) -> bool:
    if status not in BLOCK_STATUS:
        return False
    low = text[:5000].lower()
    return status in (401, 403, 429) or any(m in low for m in BLOCK_MARKERS)


def fetch_root(base: str):
    """
    Fetch the homepage, following redirects (incl. rebrands to a new domain).
    Returns (final_url, html) on success, or a verdict string on failure:
    'dead-domain' (nothing answers) / 'blocked' (bot protection).
    """
    blocked = False
    for url in (f"https://{base}", f"https://www.{base}", f"http://{base}"):
        try:
            r = session().get(url, timeout=TIMEOUT, allow_redirects=True)
        except Exception:
            continue
        if r.status_code < 400:
            if _reg(urlparse(r.url).netloc) in REJECT_HOSTS:
                return "dead-domain"      # parked/redirect trap
            return r.url, r.text
        if looks_blocked(r.status_code, r.text):
            blocked = True
    return "blocked" if blocked else "dead-domain"


def homepage_job_links(final_url: str, html: str) -> list[str]:
    """Links on the company's own homepage whose href or text smells like jobs."""
    scored = []
    for m in A_TAG.finditer(html[:400000]):
        href, text = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        hay = f"{href} {text}"
        if not JOBISH.search(hay):
            continue
        absu = urljoin(final_url, href)
        if not absu.startswith("http"):
            continue
        host = _reg(urlparse(absu).netloc)
        if host in REJECT_HOSTS:
            continue
        # prefer explicit job words in the URL path over only-in-text matches
        score = 2 if JOBISH.search(absu) else 1
        scored.append((score, absu))
    seen, ordered = set(), []
    for _, u in sorted(scored, key=lambda t: -t[0]):
        key = u.rstrip("/")
        if key not in seen:
            seen.add(key)
            ordered.append(u)
    return ordered[:MAX_HOMEPAGE_LINKS]


def probe(url: str, base: str, trusted: bool = False):
    """
    Verify that a URL is a live page with job-ish content.
    trusted=True (link came from the company's own homepage): any final host
    is fine except REJECT_HOSTS. trusted=False (blind path guess): final host
    must be the company's domain or a known ATS.
    """
    try:
        r = session().get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        final_host = _reg(urlparse(r.url).netloc)
        if final_host in REJECT_HOSTS or "login" in urlparse(r.url).path.lower():
            return None
        if not trusted and final_host != _reg(base) and final_host not in KNOWN_ATS:
            return None
        if len(JOBISH.findall(r.text[:200000])) >= 2:
            return r.url
    except Exception:
        pass
    return None


def check_company(name: str, base: str):
    start = time.time()
    root = fetch_root(base)
    if isinstance(root, str):                 # 'dead-domain' or 'blocked'
        return name, None, root
    final_url, html = root
    real_base = _reg(urlparse(final_url).netloc)  # follow rebrands (.be -> .com)

    # 1) links the company itself put on its homepage — highest signal
    for u in homepage_job_links(final_url, html):
        if time.time() - start > PER_COMPANY_BUDGET:
            return name, None, "time-budget"
        hit = probe(u, real_base, trusted=True)
        if hit:
            return name, hit, "found"

    # 2) fallback: the classic path guesses, on the company's real domain
    for pat in PATTERNS:
        if time.time() - start > PER_COMPANY_BUDGET:
            return name, None, "time-budget"
        hit = probe(pat.format(d=real_base), real_base)
        if hit:
            return name, hit, "found"
    return name, None, "no-career-page"


def flush(found, missed, skipped, done, total):
    out = ["# Auto-discovered career pages — review, then append to sources.yaml",
           "sources:"]
    for n, u in found:
        clean = n.replace('"', '').replace("'", '').strip(' -_.,;')
        clean = re.sub(r'\s+', ' ', clean) or n.strip()
        out.append(f'  - company: "{clean}"')
        out.append(f'    url: "{u}"')
    (ROOT / "discovered_sources.yaml").write_text("\n".join(out) + "\n", encoding="utf-8")
    reasons = {}
    for _, why in missed:
        reasons[why] = reasons.get(why, 0) + 1
    reason_line = " | ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) or "-"
    report = [
        f"Discovery report — {done}/{total} processed | "
        f"{len(found)} found, {len(missed)} not found, {len(skipped)} skipped",
        f"Miss reasons — {reason_line}",
        "",
        "== FOUND ==", *[f"{n}: {u}" for n, u in found],
        "", "== NOT FOUND ==", *[f"{n} ({why})" for n, why in missed],
        "", "== SKIPPED (already tracked) ==", *skipped,
    ]
    (ROOT / "discovery_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def main():
    existing = load_existing_domains()
    companies = load_companies()
    todo, skipped = [], []
    for name, base in companies:
        reg = ".".join(base.split(".")[-2:])
        if reg in existing:
            skipped.append(name)
        else:
            todo.append((name, base))
    total = len(todo)
    print(f"[discover] {total} companies to probe ({len(skipped)} already tracked)")

    found, missed, done = [], [], 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(check_company, n, b): n for n, b in todo}
        for fut in as_completed(futs):
            name, url, verdict = fut.result()
            with lock:
                done += 1
                if url:
                    found.append((name, url))
                    print(f"[{done}/{total}] FOUND {name}: {url}", flush=True)
                else:
                    missed.append((name, verdict))
                    if done % 25 == 0:
                        print(f"[{done}/{total}] …", flush=True)
                if done % 25 == 0:
                    flush(found, missed, skipped, done, total)

    flush(found, missed, skipped, done, total)
    reasons = {}
    for _, why in missed:
        reasons[why] = reasons.get(why, 0) + 1
    print(f"\n[discover] done: {len(found)} career pages found, "
          f"{len(missed)} without ({reasons}), {len(skipped)} skipped")


if __name__ == "__main__":
    main()
