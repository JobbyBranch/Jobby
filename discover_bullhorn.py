#!/usr/bin/env python3
"""
Eenmalige discovery: zoekt careerpagina's voor de Bullhorn High/Medium-accounts
(uit bullhorn.json) die nog niet in sources.yaml staan, en voegt de gevonden
pagina's toe aan sources.yaml in het bestaande formaat.

- Probeert eerst gangbare paden op het eigen domein (/jobs, /vacatures, ...along
  met jobs./careers.-subdomeinen), valideert op HTTP 200 + vacaturewoorden.
- Valt terug op Serper (SERPER_API_KEY) voor moeilijke gevallen en bedrijven
  zonder website. Budget via MAX_SERPER (default 300).
- Dedupliceert op bedrijfsnaam en hoofddomein tegen de bestaande sources.
- Schrijft een rapport naar bullhorn_discovery_report.txt.

Outputs: sources.yaml (aangevuld), bullhorn_discovery_report.txt
"""
import os, re, json, unicodedata, datetime
import requests, yaml

SERPER_KEY = os.environ.get("SERPER_API_KEY", "")
MAX_SERPER = int(os.environ.get("MAX_SERPER", "300"))
TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8"}

CAREER_PATHS = ["/jobs", "/jobs/", "/careers", "/careers/", "/vacatures", "/vacatures/",
                "/nl/jobs", "/nl/vacatures", "/nl/careers", "/en/careers", "/nl-be/jobs",
                "/werken-bij", "/nl/werken-bij", "/career", "/jobs-en-carriere", "/join-us"]
VACANCY_WORDS = re.compile(r"vacature|vacancies|vacancy|solliciteer|apply now|open positions|open roles|"
                           r"job opening|jobs bij|careers|werken bij|join (our|the) team|functie", re.I)

clean = lambda s: re.sub(r"\s+", " ", (s or "")).strip()
LEGAL = r"\b(nv|sa|bv|bvba|sprl|srl|cvba|cv|gmbh|ag|ltd|plc|inc|llc|group|groep|belgium|belgie|belgique|nederland|holding|international|europe|benelux)\b"
def norm(s):
    s = unicodedata.normalize("NFD", clean(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9 ]", " ", s); s = re.sub(LEGAL, " ", s)
    return re.sub(r"\s+", " ", s).strip()
def contains(a, b):
    if a == b: return True
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    return len(s) >= 4 and re.search(r"(^| )" + re.escape(s) + r"( |$)", l)
def host(u):
    m = re.match(r"https?://(?:www\.)?([^/]+)", u or "")
    return m.group(1).lower() if m else ""
def root(h):
    p = h.split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else h

serper_used = 0
def serper(q):
    global serper_used
    if not SERPER_KEY or serper_used >= MAX_SERPER:
        return []
    serper_used += 1
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                          json={"q": q, "gl": "be", "hl": "nl", "num": 10}, timeout=TIMEOUT)
        return [o.get("link", "") for o in r.json().get("organic", [])]
    except Exception:
        return []

def looks_like_career_page(url):
    """True als de URL bestaat en op een vacaturepagina lijkt."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 500:
            return None
        if not VACANCY_WORDS.search(r.text[:200000]):
            return None
        return r.url.rstrip("/") if r.url else url
    except Exception:
        return None

# bekende jobplatform-domeinen die als careersite van een bedrijf dienen
PLATFORMS = ("jobs.", "careers.", "werkenbij", ".recruitee.com", ".teamtailor.com", ".homerun.co",
             ".jobtoolz.com", ".cvwarehouse.com", "cvw.io", ".softgarden.io", ".workable.com",
             ".breezy.hr", ".bamboohr.com", "csod.com", "successfactors", "myworkdayjobs.com",
             ".talentfinder.be", ".jazzhr.com", "hr-technologies.com")

def discover_for(name, website):
    tried = set()
    # 1. paden en subdomeinen op het eigen domein
    if website:
        site = website if re.match(r"^https?://", website) else "https://" + website
        h = host(site); rt = root(h)
        bases = [f"https://{h}", f"https://www.{rt}", f"https://jobs.{rt}", f"https://careers.{rt}"]
        cands = []
        for b in bases[2:]:
            cands.append(b)                      # jobs./careers.-subdomein zelf
        for b in bases[:2]:
            for p in CAREER_PATHS:
                cands.append(b + p)
        for u in cands:
            if u in tried: continue
            tried.add(u)
            found = looks_like_career_page(u)
            if found:
                return found, "pad-probe"
    # 2. Serper-fallback
    rt = root(host(website)) if website else ""
    for link in serper(f'{name} vacatures jobs'):
        lh = host(link); lr = root(lh)
        related = (rt and lr == rt) or contains(norm(name), norm(lh.split(".")[0])) \
                  or any(p in link.lower() for p in PLATFORMS)
        if not related:
            continue
        if link in tried: continue
        tried.add(link)
        found = looks_like_career_page(link)
        if found:
            return found, "serper"
    return None, None

def main():
    src = yaml.safe_load(open("sources.yaml"))
    entries = src["sources"]
    src_names = {norm(e.get("company", "")) for e in entries}
    src_roots = {root(host(e.get("url", ""))) for e in entries}
    src_urls = {e.get("url", "").rstrip("/") for e in entries}

    bh = json.load(open("bullhorn.json"))
    todo = []
    for a in bh["accounts"]:
        an = norm(a["name"])
        ar = root(host(a["website"] or ""))
        if an in src_names or (ar and ar in src_roots) or any(contains(an, sn) for sn in src_names if sn):
            continue
        todo.append(a)
    todo.sort(key=lambda a: (a["potential"] != "High", (a["name"] or "").lower()))
    print(f"{len(todo)} Bullhorn-accounts ontbreken in sources — discovery start (Serper-budget {MAX_SERPER})")

    found, missed = [], []
    for i, a in enumerate(todo, 1):
        url, via = discover_for(a["name"], a["website"])
        if url and url.rstrip("/") not in src_urls and root(host(url)) not in ("linkedin.com", "indeed.com", "glassdoor.com", "vdab.be", "jobat.be", "stepstone.be", "google.com", "facebook.com"):
            found.append((a, url, via))
            src_urls.add(url.rstrip("/")); src_roots.add(root(host(url)))
        else:
            missed.append(a)
        if i % 25 == 0:
            print(f"  {i}/{len(todo)} — gevonden: {len(found)}, serper gebruikt: {serper_used}")

    if found:
        stamp = datetime.date.today().isoformat()
        with open("sources.yaml", "a", encoding="utf-8") as f:
            f.write(f"\n  # --- Bullhorn High/Medium accounts (auto-discovered {stamp}) ---\n")
            for a, url, via in found:
                cname = a["name"].replace('"', "'")
                f.write(f'  - company: "{cname}"\n    url: "{url}"\n')
        yaml.safe_load(open("sources.yaml"))   # sanity: blijft geldige YAML

    with open("bullhorn_discovery_report.txt", "w", encoding="utf-8") as f:
        f.write(f"Bullhorn source discovery {datetime.datetime.now():%Y-%m-%d %H:%M}\n")
        f.write(f"te zoeken: {len(todo)} | gevonden: {len(found)} | niet gevonden: {len(missed)} | serper gebruikt: {serper_used}\n\n")
        f.write("== TOEGEVOEGD ==\n")
        for a, url, via in found:
            f.write(f"{a['name']} ({a['potential']}, {via}): {url}\n")
        f.write("\n== NIET GEVONDEN (handmatig toevoegen of geen careerpagina) ==\n")
        for a in missed:
            f.write(f"{a['name']} ({a['potential']}) — website: {a['website'] or '-'}\n")
    print(f"Klaar: {len(found)} toegevoegd aan sources.yaml, {len(missed)} niet gevonden (zie rapport)")

if __name__ == "__main__":
    main()
