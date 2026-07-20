#!/usr/bin/env python3
"""
JobRadar company harvester — the "Company Discovery" box.

Pipeline:
  1. Download the monthly KBO/CBE open-data dump (all Belgian companies)
  2. Filter: active legal entities, Flemish postcode, relevant NACE activity
     (staffing agencies NACE 78* and IT consultancies NACE 6202* excluded)
  3. Find each company's website:
       a. KBO contact data (WEB records) — free, authoritative
       b. Serper.dev search fallback — for companies without registered site
  4. Write companies_auto.txt (Name;domain) for discover.py to verify
     career pages, and harvest_state.json so re-runs never repeat work.

Env (GitHub secrets): KBO_LOGIN, KBO_PASSWORD, SERPER_API_KEY
Optional env: MAX_NEW_COMPANIES (default 800), MAX_SERPER (default 500)
"""

import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "harvest_state.json"
OUT_FILE = ROOT / "companies_auto.txt"
KBO_BASE = "https://kbopub.economie.fgov.be/kbo-open-data"

MAX_NEW = int(os.environ.get("MAX_NEW_COMPANIES", "800"))
MAX_SERPER = int(os.environ.get("MAX_SERPER", "500"))

# Flemish postcodes: Vlaams-Brabant, Antwerpen, Limburg, West-Vl, Oost-Vl
def is_flemish(zipcode: str) -> bool:
    try:
        z = int(zipcode)
    except (TypeError, ValueError):
        return False
    return (1500 <= z <= 1999) or (2000 <= z <= 3999) or (8000 <= z <= 9999)

# NACE main-activity prefixes we exclude entirely
NACE_EXCLUDE_PREFIX = (
    "78",     # employment/staffing agencies — the hard requirement
    "6202",   # IT consultancy — competitors posting for clients
    "6420",   # holdings (empty shells)
    "68",     # real estate (mostly patrimonium vehicles)
    "01", "02", "03",  # agriculture/forestry/fishing micro-companies
    "9700", "9810", "9820", "9900",  # households
)
# NACE prefixes that get priority (likely to have real IT departments)
NACE_PRIORITY_PREFIX = (
    "10", "11", "20", "21", "22", "23", "24", "25", "26", "27", "28",
    "29", "30",                    # manufacturing & industry
    "35", "36", "37", "38",        # energy, water, waste
    "46", "47",                    # wholesale & retail
    "49", "50", "51", "52", "53",  # transport & logistics
    "58", "6201", "6203", "6209", "631",  # software product & data
    "60", "61",                    # media & telecom
    "64", "65", "66",              # finance & insurance
    "71", "72",                    # engineering offices & R&D
    "84", "85", "86",              # public admin, education, healthcare
)

SEARCH_BLACKLIST = {
    "facebook.com", "linkedin.com", "instagram.com", "youtube.com",
    "trendstop.be", "staatsbladmonitor.be", "companyweb.be", "kbo.be",
    "bizzy.org", "openthebox.be", "wikipedia.org", "goldenpages.be",
    "pagesdor.be", "infobel.com", "trends.be", "dnb.com", "kompass.com",
    "europages.com", "indeed.com", "jobat.be", "vdab.be", "glassdoor.com",
}


def log(msg):
    print(msg, flush=True)


def kbo_session() -> requests.Session:
    ses = requests.Session()
    ses.headers["User-Agent"] = "JobRadar harvester (contact: repo owner)"
    login, pw = os.environ.get("KBO_LOGIN"), os.environ.get("KBO_PASSWORD")
    if not login or not pw:
        sys.exit("KBO_LOGIN / KBO_PASSWORD secrets missing")
    # try the files index; if bounced to login, do the form login
    r = ses.get(f"{KBO_BASE}/affiliation/xml/", auth=(login, pw), timeout=30)
    if "j_spring_security_check" in r.text or r.status_code in (401, 403):
        ses.post(f"{KBO_BASE}/static/j_spring_security_check",
                 data={"j_username": login, "j_password": pw}, timeout=30)
        r = ses.get(f"{KBO_BASE}/affiliation/xml/", timeout=30)
    if r.status_code != 200:
        sys.exit(f"KBO login failed (HTTP {r.status_code}) — check the secrets")
    ses._index_html = r.text
    return ses


def download_latest_full(ses: requests.Session) -> Path:
    names = re.findall(r"KboOpenData_\d+_\d{4}_\d{2}_Full\.zip", ses._index_html)
    if not names:
        sys.exit("No Full.zip found on the KBO files page — page layout may have changed")
    latest = sorted(set(names))[-1]
    dest = ROOT / latest
    if dest.exists():
        log(f"[kbo] {latest} already downloaded")
        return dest
    log(f"[kbo] downloading {latest} (few hundred MB, be patient)…")
    with ses.get(f"{KBO_BASE}/affiliation/xml/files/{latest}", stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    log(f"[kbo] downloaded {dest.stat().st_size // (1 << 20)} MB")
    return dest


def stream_csv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def build_candidates(zip_path: Path) -> list[dict]:
    zf = zipfile.ZipFile(zip_path)
    have = {i.filename for i in zf.infolist()}
    log(f"[kbo] archive contains: {sorted(have)}")

    log("[kbo] pass 1/5: active legal entities…")
    ent_ok = set()
    for row in stream_csv(zf, "enterprise.csv"):
        if row.get("Status") == "AC" and row.get("TypeOfEnterprise") == "2":
            ent_ok.add(row["EnterpriseNumber"])
    log(f"        {len(ent_ok):,}")

    log("[kbo] pass 2/5: Flemish addresses…")
    flemish = set()
    for row in stream_csv(zf, "address.csv"):
        if row.get("TypeOfAddress") == "REGO" and row["EntityNumber"] in ent_ok \
                and is_flemish(row.get("Zipcode", "")):
            flemish.add(row["EntityNumber"])
    log(f"        {len(flemish):,}")

    log("[kbo] pass 3/5: NACE filter…")
    keep, priority = set(), set()
    for row in stream_csv(zf, "activity.csv"):
        n = row["EntityNumber"]
        if n not in flemish or row.get("Classification") != "MAIN":
            continue
        code = row.get("NaceCode", "")
        if any(code.startswith(p) for p in NACE_EXCLUDE_PREFIX):
            keep.discard(n)
            priority.discard(n)
            flemish.discard(n)  # hard exclusion
            continue
        keep.add(n)
        if any(code.startswith(p) for p in NACE_PRIORITY_PREFIX):
            priority.add(n)
    log(f"        kept {len(keep):,} (priority {len(priority):,})")

    log("[kbo] pass 4/5: names…")
    names = {}
    for row in stream_csv(zf, "denomination.csv"):
        n = row["EntityNumber"]
        if n not in keep:
            continue
        t = row.get("TypeOfDenomination")
        # prefer commercial name (003) over legal name (001)
        if t == "003" or n not in names:
            names[n] = row.get("Denomination", "").strip()

    log("[kbo] pass 5/5: registered websites…")
    webs = {}
    if "contact.csv" in have:
        for row in stream_csv(zf, "contact.csv"):
            n = row["EntityNumber"]
            if n in keep and row.get("ContactType") == "WEB" and n not in webs:
                webs[n] = row.get("Value", "").strip()
    log(f"        {len(webs):,} companies with registered website")

    out = []
    for n in keep:
        nm = names.get(n)
        if not nm or len(nm) < 3:
            continue
        out.append({"nr": n, "name": nm, "web": webs.get(n, ""),
                    "prio": 1 if n in priority else 0})
    # order: registered-website + priority sector first
    out.sort(key=lambda c: (-(bool(c["web"])), -c["prio"], c["name"]))
    return out


def clean_domain(url: str) -> str | None:
    if not url:
        return None
    u = url if url.startswith("http") else f"https://{url}"
    host = urlparse(u).netloc.lower().replace("www.", "")
    if not host or "." not in host:
        return None
    reg = ".".join(host.split(".")[-2:])
    if reg in SEARCH_BLACKLIST:
        return None
    return host


def serper_lookup(name: str, key: str) -> str | None:
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": key, "Content-Type": "application/json"},
                          json={"q": f'"{name}" bedrijf België', "gl": "be", "hl": "nl", "num": 5},
                          timeout=15)
        r.raise_for_status()
        for item in r.json().get("organic", []):
            dom = clean_domain(item.get("link", ""))
            if not dom:
                continue
            # crude sanity check: some overlap between company name and domain
            base = re.sub(r"[^a-z0-9]", "", name.lower())[:12]
            flat = re.sub(r"[^a-z0-9]", "", dom.split(".")[0])
            if base[:6] in flat or flat[:6] in base or len(set(base) & set(flat)) >= min(5, len(flat)):
                return dom
        return None
    except requests.RequestException:
        return None


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"done": []}
    done = set(state["done"])

    ses = kbo_session()
    zip_path = download_latest_full(ses)
    cands = [c for c in build_candidates(zip_path) if c["nr"] not in done]
    log(f"[harvest] {len(cands):,} candidates not yet processed")

    serper_key = os.environ.get("SERPER_API_KEY", "")
    serper_used = 0
    batch, lines = 0, []
    for c in cands:
        if batch >= MAX_NEW:
            break
        dom = clean_domain(c["web"])
        if not dom and serper_key and serper_used < MAX_SERPER:
            dom = serper_lookup(c["name"], serper_key)
            serper_used += 1
            time.sleep(0.25)
        done.add(c["nr"])
        if dom:
            safe_name = c["name"].replace(";", ",")
            lines.append(f"{safe_name};{dom}")
            batch += 1
            if batch % 50 == 0:
                log(f"[harvest] {batch} companies resolved (serper used: {serper_used})")

    existing = OUT_FILE.read_text().splitlines() if OUT_FILE.exists() else []
    seen_domains = {l.split(";")[-1] for l in existing if ";" in l}
    added = [l for l in lines if l.split(";")[-1] not in seen_domains]
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        for l in added:
            f.write(l + "\n")

    state["done"] = sorted(done)
    STATE_FILE.write_text(json.dumps(state))
    log(f"[harvest] wrote {len(added)} new companies to {OUT_FILE.name} "
        f"(serper lookups: {serper_used}); total in file: {len(existing) + len(added)}")
    log("[harvest] next step: run the source discovery workflow to verify career pages")


if __name__ == "__main__":
    main()
