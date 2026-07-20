#!/usr/bin/env python3
"""
JobRadar source discovery
─────────────────────────
Reads companies.txt (lines of "Company Name;domain"), probes each company's
likely career-page URLs, verifies which one actually serves a jobs page, and
writes:

  discovered_sources.yaml  -> ready-to-append entries for sources.yaml
  discovery_report.txt     -> per-company result (found / not found / skipped)

Companies whose domain already appears in sources.yaml are skipped, so you
can re-run this safely after adding more names to companies.txt.

Run manually via the "JobRadar source discovery" workflow in GitHub Actions.
"""

import re
import time
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

# Probed in this order — first verified hit wins
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
    "https://www.{d}/nl/careers",
    "https://www.{d}/werken-bij",
    "https://www.{d}/nl/werken-bij",
    "https://www.{d}/jobs-en-carriere",
    "https://{d}/jobs",
    "https://{d}/careers",
    "https://{d}/vacatures",
]

JOBISH = re.compile(
    r"(vacature|vacatures|job|jobs|career|careers|solliciteer|werken bij|"
    r"join (our|the) team|open positions|opportunit)", re.I)


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


def probe(url: str):
    """Return (final_url, ok) — ok means the page exists and smells like jobs."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if r.status_code >= 400:
            return None, False
        text = r.text[:200000]
        # a jobs page should mention jobs; a soft-404 landing page usually won't
        hits = len(JOBISH.findall(text))
        return r.url, hits >= 2
    except requests.RequestException:
        return None, False


def main():
    existing = load_existing_domains()
    lines = []
    for fname in ("companies.txt", "companies_auto.txt"):
        p = ROOT / fname
        if p.exists():
            lines += [l.strip() for l in p.read_text(encoding="utf-8").splitlines()]
    found, missed, skipped = [], [], []

    for line in lines:
        if not line or line.startswith("#") or ";" not in line:
            continue
        name, domain = [p.strip() for p in line.split(";", 1)]
        base = domain.lower().replace("www.", "")
        reg = ".".join(base.split(".")[-2:])
        if reg in existing:
            skipped.append(f"{name} — already in sources.yaml")
            print(f"[skip] {name} (already tracked)")
            continue

        print(f"[probe] {name} ({domain})")
        hit = None
        for pat in PATTERNS:
            url = pat.format(d=base)
            final, ok = probe(url)
            if ok:
                hit = final
                break
            time.sleep(0.2)

        if hit:
            found.append((name, hit))
            print(f"   -> FOUND: {hit}")
        else:
            missed.append(name)
            print("   -> not found")
        time.sleep(0.4)

    out = ["# Auto-discovered career pages — review, then append to sources.yaml", "sources:"]
    for name, url in found:
        out.append(f'  - company: "{name}"')
        out.append(f'    url: "{url}"')
    (ROOT / "discovered_sources.yaml").write_text("\n".join(out) + "\n", encoding="utf-8")

    report = [
        f"Discovery report — {len(found)} found, {len(missed)} not found, {len(skipped)} skipped",
        "",
        "== FOUND ==",
        *[f"{n}: {u}" for n, u in found],
        "",
        "== NOT FOUND (needs a manual look) ==",
        *missed,
        "",
        "== SKIPPED (already tracked) ==",
        *skipped,
    ]
    (ROOT / "discovery_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\nDone: {len(found)} found, {len(missed)} missed, {len(skipped)} skipped")


if __name__ == "__main__":
    main()
