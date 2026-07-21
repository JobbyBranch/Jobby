#!/usr/bin/env python3
"""
JobRadar source discovery — v2 (parallel, fast-fail, incremental)

Probes each company's likely career-page URLs and keeps only verified job
pages. Built to chew through harvested registry batches:

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
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).parent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 JobRadar/1.0",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
}
TIMEOUT = 8
PER_COMPANY_BUDGET = 45   # seconds, hard cap
WORKERS = 12

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

JOBISH = re.compile(
    r"(vacature|vacatures|job|jobs|career|careers|solliciteer|werken bij|"
    r"join (our|the) team|open positions|opportunit)", re.I)

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


def root_alive(base: str) -> bool:
    """One cheap check: does anything answer at all on this domain?"""
    for scheme in ("https", "http"):
        try:
            r = session().get(f"{scheme}://{base}", timeout=6, allow_redirects=True)
            if r.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False


def probe(url: str):
    try:
        r = session().get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        if len(JOBISH.findall(r.text[:200000])) >= 2:
            return r.url
    except requests.RequestException:
        pass
    return None


def check_company(name: str, base: str):
    start = time.time()
    if not root_alive(base):
        return name, None, "dead-domain"
    for pat in PATTERNS:
        if time.time() - start > PER_COMPANY_BUDGET:
            return name, None, "time-budget"
        hit = probe(pat.format(d=base))
        if hit:
            return name, hit, "found"
    return name, None, "no-career-page"


def flush(found, missed, skipped, done, total):
    out = ["# Auto-discovered career pages — review, then append to sources.yaml",
           "sources:"]
    for n, u in found:
        out.append(f'  - company: "{n}"')
        out.append(f'    url: "{u}"')
    (ROOT / "discovered_sources.yaml").write_text("\n".join(out) + "\n", encoding="utf-8")
    report = [
        f"Discovery report — {done}/{total} processed | "
        f"{len(found)} found, {len(missed)} not found, {len(skipped)} skipped",
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
    print(f"\n[discover] done: {len(found)} career pages found, "
          f"{len(missed)} without, {len(skipped)} skipped")


if __name__ == "__main__":
    main()
