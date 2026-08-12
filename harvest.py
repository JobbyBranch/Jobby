#!/usr/bin/env python3
"""
JobRadar company harvester — the "Company Discovery" box.

Pipeline:
  1. Download the monthly KBO/CBE open-data dump (all Belgian companies)
  2. Filter: active legal entities, Flemish postcode, relevant NACE activity.
     The NACE filter is VERSION-AWARE: since March 2025 the KBO carries
     NACE-BEL 2025 codes next to (or instead of) the 2008 ones, and the
     numbering differs (IT consultancy: 6202 -> 6220, garages: 45 -> 95).
     Hard exclusions (either code version triggers them):
       staffing 78*, IT consultancy, holdings, real estate, agriculture,
       garages/car trade, pharmacies, horeca, hairdressers & other
       personal services, households.
  3. CENSUS ("de telling"): before any Serper credit is spent, log how many
     candidates remain, split by website / legal form / priority sector —
     so the Serper purchase decision rests on numbers, not assumptions.
     Set COUNT_ONLY=1 to stop right after the census (nothing written,
     nothing spent).
  4. Find each company's website:
       a. KBO contact data (WEB records) — free, authoritative
       b. Serper.dev search fallback — ONLY for companies worth a credit
          (legal form NV/SE/BV/CV, or a priority-sector VZW/other)
  5. Write companies_auto.txt (Name;domain) for discover.py to verify
     career pages, and harvest_state.json so re-runs never repeat work.

Processing order: registered website first, then legal-form tier
(NV/SE > BV/CV > VZW > rest), then priority sector — biggest fish first.

Env (GitHub secrets): KBO_LOGIN, KBO_PASSWORD, SERPER_API_KEY
Optional env: MAX_NEW_COMPANIES (default 800), MAX_SERPER (default 500),
              COUNT_ONLY (set to 1 for a census-only dry run)
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
COUNT_ONLY = os.environ.get("COUNT_ONLY", "").strip() in ("1", "true", "yes")

# Flemish postcodes: Vlaams-Brabant, Antwerpen, Limburg, West-Vl, Oost-Vl
def is_flemish(zipcode: str) -> bool:
    try:
        z = int(zipcode)
    except (TypeError, ValueError):
        return False
    return (1500 <= z <= 1999) or (2000 <= z <= 3999) or (8000 <= z <= 9999)


# ---------------------------------------------------------------------------
# NACE filters — one set per code version present in the dump.
# activity.csv rows carry a NaceVersion column ("2003" / "2008" / "2025");
# an entity is hard-excluded as soon as ANY of its MAIN activities matches
# the exclude list of that row's version.
# ---------------------------------------------------------------------------

# --- NACE-BEL 2008 ---------------------------------------------------------
NACE_EXCLUDE_2008 = (
    "78",     # employment/staffing agencies — the hard requirement
    "6202",   # IT consultancy — competitors posting for clients
    "6420",   # holdings (empty shells)
    "68",     # real estate (mostly patrimonium vehicles)
    "01", "02", "03",  # agriculture/forestry/fishing micro-companies
    "45",     # car trade & garages           (junk seen in batch 1)
    "4773",   # pharmacies                    (junk seen in batch 1)
    "55", "56",        # horeca               (junk seen in batch 1)
    "96",     # hairdressers, beauty, laundry (junk seen in batch 1)
    "9700", "9810", "9820", "9900",  # households
)
NACE_PRIORITY_2008 = (
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

# --- NACE-BEL 2025 (Rev 2.1) -----------------------------------------------
# Renumbering that matters here:
#   62.02 IT consultancy      -> 62.20
#   45    garages/car repair  -> repair moved to division 95, trade into 46/47
#   division 45 no longer exists; households are plain divisions 97/98/99
NACE_EXCLUDE_2025 = (
    "78",     # staffing — unchanged division
    "6220",   # IT consultancy (was 6202)
    "6420",   # holdings — unchanged
    "68",     # real estate — unchanged
    "01", "02", "03",  # agriculture — unchanged
    "4773",   # pharmacies (product-based retail keeps the class)
    "55", "56",        # horeca — unchanged
    "95",     # repair incl. garages (motor-vehicle repair moved here)
    "96",     # personal services — unchanged division
    "97", "98", "99",  # households & extraterritorial
)
NACE_PRIORITY_2025 = (
    "10", "11", "20", "21", "22", "23", "24", "25", "26", "27", "28",
    "29", "30",
    "35", "36", "37", "38",
    "46", "47",
    "49", "50", "51", "52", "53",
    "58", "62", "631",   # "62" is safe: 6220 entities are already discarded
    "60", "61",
    "64", "65", "66",
    "71", "72",
    "84", "85", "86",
)

NACE_SETS = {
    "2008": (NACE_EXCLUDE_2008, NACE_PRIORITY_2008),
    "2025": (NACE_EXCLUDE_2025, NACE_PRIORITY_2025),
    # "2003" rows are ignored: Rev 1.1 numbering is incompatible (staffing
    # was 74.5 back then) and every active entity also carries 2008/2025 rows.
}

# Legal-form tiers (bigger form = bigger company, statistically).
# Built from code.csv descriptions so we never depend on memorised codes.
FORM_TIER_NAMES = {3: "NV/SE", 2: "BV/CV", 1: "VZW", 0: "overig"}
SERPER_MIN_TIER = 2   # spend a credit on tier>=2, or on priority-sector below

SEARCH_BLACKLIST = {
    "facebook.com", "linkedin.com", "instagram.com", "youtube.com",
    "trendstop.be", "staatsbladmonitor.be", "companyweb.be", "kbo.be",
    "bizzy.org", "openthebox.be", "wikipedia.org", "goldenpages.be",
    "pagesdor.be", "infobel.com", "trends.be", "dnb.com", "kompass.com",
    "europages.com", "indeed.com", "jobat.be", "vdab.be", "glassdoor.com",
}


def log(msg):
    print(msg, flush=True)


LOGIN_MARKERS = ("j_username", "j_password", "wachtwoord", "password", "aanmelden", "log in", "login")

def _looks_like_login_page(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in LOGIN_MARKERS[:2]) or ("<form" in low and any(m in low for m in LOGIN_MARKERS))


def _parse_form(html: str):
    """Extract (action, fields) from the first <form> in the page."""
    m = re.search(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I)
    if not m:
        return None, {}
    form_tag = html[m.start():html.index(">", m.start()) + 1]
    am = re.search(r"action\s*=\s*[\"']([^\"']*)[\"']", form_tag, re.I)
    action = am.group(1) if am else ""
    fields = {}
    for inp in re.finditer(r"<input\b[^>]*>", m.group(1), re.I):
        tag = inp.group(0)
        name = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        if not name:
            continue
        value = re.search(r"value\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        typ = re.search(r"type\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        fields[name.group(1)] = {"value": value.group(1) if value else "",
                                 "type": (typ.group(1).lower() if typ else "text")}
    return action, fields


def kbo_session() -> requests.Session:
    from urllib.parse import urljoin
    ses = requests.Session()
    ses.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) JobRadar harvester"
    login, pw = os.environ.get("KBO_LOGIN"), os.environ.get("KBO_PASSWORD")
    if not login or not pw:
        sys.exit("KBO_LOGIN / KBO_PASSWORD secrets missing")

    files_url = os.environ.get("KBO_FILES_URL", f"{KBO_BASE}/affiliation/xml/?files")

    # 1. load the page -> login form + session cookies
    r = ses.get(files_url, timeout=30)
    if "Full.zip" in r.text:
        ses._index_html, ses._index_url = r.text, files_url
        log("[kbo] already authenticated (no login needed)")
        return ses

    # 2. parse the real form and fill it
    action, fields = _parse_form(r.text)
    if not fields:
        sys.exit("[kbo] no login form found on the page — layout changed, need a new look")
    payload = {}
    user_set = pass_set = False
    for name, meta in fields.items():
        low = name.lower()
        if meta["type"] == "password":
            payload[name] = pw; pass_set = True
        elif not user_set and (meta["type"] in ("text", "email")) and \
                any(k in low for k in ("user", "login", "mail", "name", "id")):
            payload[name] = login; user_set = True
        else:
            payload[name] = meta["value"]  # hidden tokens (CSRF, flow ids) echoed back
    if not user_set:  # fallback: first text input gets the username
        for name, meta in fields.items():
            if meta["type"] in ("text", "email"):
                payload[name] = login; user_set = True; break
    log(f"[kbo] login form: action='{action or '(self)'}', fields={sorted(fields)}, "
        f"user-field-found={user_set}, password-field-found={pass_set}")
    if not pass_set:
        sys.exit("[kbo] no password field in the form — page is not a standard login form")

    submit_url = urljoin(r.url, action) if action else r.url
    ses.headers["Referer"] = r.url
    pr = ses.post(submit_url, data=payload, timeout=30, allow_redirects=True)
    log(f"[kbo] submitted login to {submit_url}: HTTP {pr.status_code}")
    log(f"[kbo] post-login landed on: {pr.url}")
    low = (pr.url + " " + pr.text[:3000]).lower()
    if any(k in low for k in ("login_error", "bad credentials", "badcredentials", "authentication failed", "onjuist", "incorrect")):
        sys.exit("[kbo] VERDICT: the server explicitly rejected the credentials — "
                 "the KBO_LOGIN / KBO_PASSWORD secrets don't match a working login. "
                 "Verify in an incognito browser window, then overwrite both secrets.")

    # 3. fetch the files page with the authenticated session
    r2 = ses.get(files_url, timeout=30)
    if "Full.zip" in r2.text:
        ses._index_html, ses._index_url = r2.text, files_url
        log("[kbo] login OK — files page visible")
        return ses
    if "Full.zip" in pr.text:
        ses._index_html, ses._index_url = pr.text, submit_url
        log("[kbo] login OK — files listed in post-login page")
        return ses

    txt = re.sub(r"<[^>]+>", " ", r2.text)
    txt = re.sub(r"\s+", " ", txt)[:400]
    log(f"[kbo][debug] after login still no files; page says: {txt}")
    sys.exit("[kbo] login submitted but files page still not visible — "
             "likely wrong credentials or an extra login step; see debug above")


def download_latest_full(ses: requests.Session) -> Path:
    names = re.findall(r"KboOpenData_[\w.]*?\d{4}_\d{2}[\w.]*?_Full\.zip", ses._index_html)
    if not names:
        sys.exit("Files page loaded but no Full.zip filename matched — layout changed")
    latest = sorted(set(names))[-1]
    dest = ROOT / latest
    if dest.exists():
        log(f"[kbo] {latest} already downloaded")
        return dest
    log(f"[kbo] downloading {latest} (few hundred MB, be patient)…")
    base = getattr(ses, "_index_url", f"{KBO_BASE}/affiliation/xml/")
    dl_base = base.split("?")[0].rstrip("/")
    if not dl_base.endswith("/files"):
        dl_base += "/files"
    dl = f"{dl_base}/{latest}"
    log(f"[kbo] GET {dl}")
    with ses.get(dl, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    log(f"[kbo] downloaded {dest.stat().st_size // (1 << 20)} MB")
    return dest


def stream_csv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def load_form_tiers(zf: zipfile.ZipFile, have: set) -> dict:
    """
    Map JuridicalForm code -> tier, by matching the Dutch descriptions in
    code.csv (robust against the numeric codes shifting between dumps).
      3 = NV / Europese vennootschap (SE)   — the big-company forms
      2 = BV/BVBA / coöperatieve vennootschap
      1 = VZW (hospitals, schools — big employers hide here)
      0 = everything else (VOF, CommV, maatschap, …)
    """
    tiers = {}
    if "code.csv" not in have:
        log("[kbo] WARNING: code.csv missing from dump — legal-form sorting disabled")
        return tiers
    for row in stream_csv(zf, "code.csv"):
        if row.get("Category") != "JuridicalForm" or row.get("Language") != "NL":
            continue
        d = row.get("Description", "").lower()
        code = row.get("Code", "")
        if "naamloze vennootschap" in d or "europese vennootschap" in d:
            tiers[code] = 3
        elif "besloten vennootschap" in d or "coöperatieve vennootschap" in d \
                or "cooperatieve vennootschap" in d:
            tiers[code] = 2
        elif "vereniging zonder winstoogmerk" in d:
            tiers[code] = 1
    log(f"[kbo] legal-form tiers mapped for {len(tiers)} form codes "
        f"(NV/SE: {sum(1 for t in tiers.values() if t == 3)}, "
        f"BV/CV: {sum(1 for t in tiers.values() if t == 2)}, "
        f"VZW: {sum(1 for t in tiers.values() if t == 1)})")
    return tiers


def build_candidates(zip_path: Path) -> list[dict]:
    zf = zipfile.ZipFile(zip_path)
    have = {i.filename for i in zf.infolist()}
    log(f"[kbo] archive contains: {sorted(have)}")

    form_tiers = load_form_tiers(zf, have)

    log("[kbo] pass 1/5: active legal entities (+ legal form)…")
    ent_form = {}   # EnterpriseNumber -> tier
    for row in stream_csv(zf, "enterprise.csv"):
        if row.get("Status") == "AC" and row.get("TypeOfEnterprise") == "2":
            ent_form[row["EnterpriseNumber"]] = form_tiers.get(row.get("JuridicalForm", ""), 0)
    log(f"        {len(ent_form):,}")

    log("[kbo] pass 2/5: Flemish addresses…")
    flemish = set()
    for row in stream_csv(zf, "address.csv"):
        if row.get("TypeOfAddress") == "REGO" and row["EntityNumber"] in ent_form \
                and is_flemish(row.get("Zipcode", "")):
            flemish.add(row["EntityNumber"])
    log(f"        {len(flemish):,}")

    log("[kbo] pass 3/5: NACE filter (version-aware: 2008 + 2025)…")
    keep, priority = set(), set()
    ver_seen = {}
    for row in stream_csv(zf, "activity.csv"):
        n = row["EntityNumber"]
        if n not in flemish or row.get("Classification") != "MAIN":
            continue
        sets = NACE_SETS.get(row.get("NaceVersion", ""))
        if sets is None:          # 2003 rows etc. — numbering incompatible
            continue
        exclude, prio_set = sets
        ver_seen[row["NaceVersion"]] = ver_seen.get(row["NaceVersion"], 0) + 1
        code = row.get("NaceCode", "")
        if any(code.startswith(p) for p in exclude):
            keep.discard(n)
            priority.discard(n)
            flemish.discard(n)    # hard exclusion — either version triggers it
            continue
        keep.add(n)
        if any(code.startswith(p) for p in prio_set):
            priority.add(n)
    log(f"        kept {len(keep):,} (priority {len(priority):,}); "
        f"MAIN rows seen per NACE version: {ver_seen}")

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
                    "dom": clean_domain(webs.get(n, "")),
                    "prio": 1 if n in priority else 0,
                    "form": ent_form.get(n, 0)})
    # order: registered website first, then legal-form tier, then sector
    out.sort(key=lambda c: (-(bool(c["dom"])), -c["form"], -c["prio"], c["name"]))
    return out


def census(cands: list[dict], serper_budget: int):
    """De telling — printed BEFORE a single Serper credit is spent."""
    def by_form(lst):
        return " | ".join(f"{FORM_TIER_NAMES[t]}: {sum(1 for c in lst if c['form'] == t):,}"
                          for t in (3, 2, 1, 0))

    with_web = [c for c in cands if c["dom"]]
    no_web = [c for c in cands if not c["dom"]]
    serper_worthy = [c for c in no_web if c["form"] >= SERPER_MIN_TIER or c["prio"]]

    log("")
    log("[telling] ══════ censusrapport — vóór enige Serper-uitgave ══════")
    log(f"[telling] kandidaten nog te verwerken:        {len(cands):,}")
    log(f"[telling]   rechtsvorm:                       {by_form(cands)}")
    log(f"[telling]   prioriteitssector:                {sum(1 for c in cands if c['prio']):,}")
    log(f"[telling] met geregistreerde website (GRATIS): {len(with_web):,}")
    log(f"[telling]   rechtsvorm:                       {by_form(with_web)}")
    log(f"[telling] zonder website:                     {len(no_web):,}")
    log(f"[telling]   Serper-waardig (NV/BV/CV of prioriteitssector): {len(serper_worthy):,}")
    log(f"[telling]   niet Serper-waardig (wordt overgeslagen):       {len(no_web) - len(serper_worthy):,}")
    log(f"[telling] → Serper-credits nodig om ALLE waardige te doen:  {len(serper_worthy):,}")
    log(f"[telling] → gratis voorraad dekt nog {len(with_web) // max(MAX_NEW, 1)} runs van {MAX_NEW}"
        if with_web else "[telling] → geen gratis voorraad meer — vanaf nu is elke resolutie een Serper-credit")
    log(f"[telling] huidige run: max {MAX_NEW} bedrijven, Serper-budget {serper_budget}")
    log("[telling] ═══════════════════════════════════════════════════════")
    log("")


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
    census(cands, MAX_SERPER if serper_key else 0)

    if COUNT_ONLY:
        log("[harvest] COUNT_ONLY gezet — stop na de telling. "
            "Niets weggeschreven, geen credit uitgegeven.")
        return

    serper_used = serper_skipped = 0
    batch, lines = 0, []
    for c in cands:
        if batch >= MAX_NEW:
            break
        dom = c["dom"]
        if not dom and serper_key and serper_used < MAX_SERPER:
            if c["form"] >= SERPER_MIN_TIER or c["prio"]:
                dom = serper_lookup(c["name"], serper_key)
                serper_used += 1
                time.sleep(0.25)
            else:
                serper_skipped += 1   # not worth a credit — mark done, move on
        done.add(c["nr"])
        if dom:
            safe_name = c["name"].replace(";", ",")
            lines.append(f"{safe_name};{dom}")
            batch += 1
            if batch % 50 == 0:
                log(f"[harvest] {batch} companies resolved "
                    f"(serper used: {serper_used}, skipped as not-worth-it: {serper_skipped})")

    existing = OUT_FILE.read_text().splitlines() if OUT_FILE.exists() else []
    seen_domains = {l.split(";")[-1] for l in existing if ";" in l}
    added = [l for l in lines if l.split(";")[-1] not in seen_domains]
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        for l in added:
            f.write(l + "\n")

    state["done"] = sorted(done)
    STATE_FILE.write_text(json.dumps(state))
    log(f"[harvest] wrote {len(added)} new companies to {OUT_FILE.name} "
        f"(serper lookups: {serper_used}, skipped as not-worth-it: {serper_skipped}); "
        f"total in file: {len(existing) + len(added)}")
    log("[harvest] next step: run the source discovery workflow to verify career pages")


if __name__ == "__main__":
    main()
