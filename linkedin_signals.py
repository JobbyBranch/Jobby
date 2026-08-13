#!/usr/bin/env python3
"""
JobRadar — LinkedIn signals

Finds public LinkedIn posts in which hiring managers ask their network for
(freelance) IT people — e.g. "URGENT: wie kent een developer met agentic
AI-ervaring?" — WITHOUT touching LinkedIn itself.

How: Google indexes public LinkedIn posts. We ask Google (via the Serper
API we already pay for) a battery of hiring-language queries, restricted
to the past day. Each hit is a public post with title, snippet and a link
straight to LinkedIn, where the recruiter clicks and replies as a human.

Honest limits: only public posts, only what Google indexed (hours to a day
of delay, never 100% coverage). Hiring-manager posts are almost always
public — they want reach — so the visible top of the iceberg is exactly
the part we care about.

Outputs:
  linkedin_signals.json        rolling feed (last 21 days) for the dashboard
  linkedin_signals_state.json  post-ids already seen (never show twice)

Env: SERPER_API_KEY (required)
Optional env: MAX_SIGNAL_QUERIES (default: all), SIGNAL_NUM (results/query, default 10)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).parent
OUT_FILE = ROOT / "linkedin_signals.json"
STATE_FILE = ROOT / "linkedin_signals_state.json"

FEED_DAYS = 21      # rolling window shown in the dashboard
SEEN_DAYS = 90      # how long we remember a post id (dedup memory)
NUM = int(os.environ.get("SIGNAL_NUM", "10"))

# ── The query battery ──────────────────────────────────────────────────
# Each query targets public posts (site:linkedin.com/posts) + hiring
# language. NL first (our market), then EN with a Belgium anchor.
QUERIES = [
    # NL — referral-style asks
    'site:linkedin.com/posts "wie kent" (developer OR engineer OR IT)',
    'site:linkedin.com/posts "ken jij" freelance (developer OR consultant)',
    'site:linkedin.com/posts "iemand in mijn netwerk" (developer OR IT OR data)',
    # NL — direct freelance searches
    'site:linkedin.com/posts "op zoek naar" freelance (developer OR engineer OR analist)',
    'site:linkedin.com/posts freelancer gezocht (IT OR developer OR data)',
    'site:linkedin.com/posts "freelance opdracht" (developer OR engineer OR architect)',
    'site:linkedin.com/posts interim (IT OR "project manager" OR analist) gezocht',
    # NL — urgency
    'site:linkedin.com/posts (urgent OR dringend) (freelancer OR consultant) (developer OR IT)',
    'site:linkedin.com/posts "per direct" gezocht (developer OR engineer)',
    # EN — anchored to Belgium so we skip the global noise
    'site:linkedin.com/posts "looking for" freelance developer (Belgium OR Antwerp OR Ghent OR Brussels)',
    'site:linkedin.com/posts "anyone in my network" (developer OR engineer) (Belgium OR Flanders)',
    'site:linkedin.com/posts urgent freelance (developer OR "data engineer" OR devops) Belgium',
]

# ── Scoring vocabulary ─────────────────────────────────────────────────
FREELANCE = re.compile(r"(freelanc|zelfstandig|interim|consultant|contractor|zzp|"
                       r"dagtarief|day ?rate|opdracht)", re.I)
URGENT = re.compile(r"(urgent|dringend|asap|per direct|zo snel mogelijk|"
                    r"snel schakelen|deze week|start (maandag|volgende))", re.I)
ASK = re.compile(r"(wie kent|ken jij|kent iemand|iemand in (mijn|je) netwerk|"
                 r"who knows|anyone in (my|your) network|op zoek naar|"
                 r"looking for|gezocht|tips? (zijn |is )?welkom)", re.I)
IT = re.compile(r"(developer|dev\b|engineer|software|programmeur|data|devops|"
                r"cloud|azure|aws|java\b|python|\.net|c#|react|angular|node|"
                r"php|sap\b|salesforce|security|cyber|analist|analyst|architect|"
                r"scrum|agile|tester|qa\b|ai\b|machine learning|agentic|iot|erp|"
                r"infrastructuur|netwerkbeheer|sysadmin|it[- ])", re.I)
# posts we do NOT want: candidates offering themselves, recruiters advertising
NOISE = re.compile(r"(ik ben (beschikbaar|op zoek naar een (nieuwe )?(opdracht|uitdaging))|"
                   r"open to work|beschikbaar voor (een )?nieuwe opdracht|"
                   r"mijn (cv|profiel)|i am available|new opportunity for me)", re.I)


def log(msg):
    print(msg, flush=True)


def post_id(url: str) -> str | None:
    """Canonical id for a LinkedIn post URL (dedup across URL variants)."""
    p = urlparse(url)
    if "linkedin.com" not in p.netloc.lower() or "/posts/" not in p.path:
        return None
    m = re.search(r"activity-?(\d{10,})", url)
    if m:
        return m.group(1)
    return p.path.rstrip("/").split("/")[-1].lower() or None


def parse_author(title: str) -> tuple[str, str]:
    """Google titles look like 'Tine Van Cauteren on LinkedIn: URGENT! …'."""
    m = re.match(r"(.{2,60}?)\s+(?:on|op)\s+LinkedIn:?\s*(.*)", title, re.I)
    if m:
        return m.group(1).strip(), (m.group(2).strip() or title)
    return "", title


def score_post(text: str) -> tuple[int, list[str]]:
    tags, score = [], 0
    if IT.search(text):        score += 3; tags.append("IT")
    if FREELANCE.search(text): score += 3; tags.append("freelance")
    if ASK.search(text):       score += 2; tags.append("vraag")
    if URGENT.search(text):    score += 2; tags.append("urgent")
    if NOISE.search(text):     score -= 4
    return score, tags


def serper(query: str, key: str) -> list[dict]:
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": key, "Content-Type": "application/json"},
                          json={"q": query, "gl": "be", "hl": "nl",
                                "num": NUM, "tbs": "qdr:d"},
                          timeout=20)
        r.raise_for_status()
        return r.json().get("organic", [])
    except requests.RequestException as e:
        log(f"[signals] query failed ({e}): {query[:60]}…")
        return []


def main():
    key = os.environ.get("SERPER_API_KEY", "")
    if not key:
        sys.exit("[signals] SERPER_API_KEY missing")

    now = datetime.now(timezone.utc)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"seen": {}}
    # prune dedup memory
    cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    seen = state["seen"]

    feed = []
    if OUT_FILE.exists():
        feed = json.loads(OUT_FILE.read_text()).get("signals", [])
    feed_cutoff = (now - timedelta(days=FEED_DAYS)).isoformat()
    feed = [s for s in feed if s.get("found_at", "") >= feed_cutoff]

    queries = QUERIES[: int(os.environ.get("MAX_SIGNAL_QUERIES", len(QUERIES)))]
    new, used = [], 0
    for q in queries:
        used += 1
        for item in serper(q, key):
            url = item.get("link", "")
            pid = post_id(url)
            if not pid or pid in seen:
                continue
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            author, headline = parse_author(title)
            score, tags = score_post(f"{title} {snippet}")
            seen[pid] = now.isoformat()
            if score < 5:          # must at least combine IT with ask/freelance
                continue
            new.append({
                "id": pid,
                "url": url.split("?")[0],
                "author": author,
                "title": headline[:220],
                "snippet": snippet[:400],
                "score": score,
                "tags": tags,
                "found_at": now.isoformat(),
            })
        time.sleep(0.3)

    # newest day first, highest score within the day
    feed = new + feed
    feed.sort(key=lambda s: (s["found_at"][:10], s["score"]), reverse=True)
    OUT_FILE.write_text(json.dumps(
        {"generated_at": now.isoformat(), "signals": feed},
        ensure_ascii=False, indent=1))
    STATE_FILE.write_text(json.dumps(state))
    log(f"[signals] {used} queries → {len(new)} new signals "
        f"(feed now {len(feed)} posts over {FEED_DAYS} days)")


if __name__ == "__main__":
    main()
