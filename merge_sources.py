#!/usr/bin/env python3
"""
Auto-merge discovered career pages into sources.yaml.

Replaces the manual review-and-paste step in the nightly pipeline:
  - dedupes on registrable domain (never adds a company twice)
  - appends new entries at the bottom of sources.yaml
  - logs every addition with a date to sources_added.log so a human
    can audit afterwards and remove anything that doesn't belong
"""

from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).parent
SRC = ROOT / "sources.yaml"
DISC = ROOT / "discovered_sources.yaml"
LOG = ROOT / "sources_added.log"


def reg(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def main():
    if not DISC.exists():
        print("[merge] no discovered_sources.yaml — nothing to merge")
        return
    current = yaml.safe_load(SRC.read_text(encoding="utf-8")) or {"sources": []}
    discovered = yaml.safe_load(DISC.read_text(encoding="utf-8")) or {}
    existing_domains = {reg(e["url"]) for e in current.get("sources", [])}

    added = []
    for entry in discovered.get("sources", []):
        d = reg(entry["url"])
        if d and d not in existing_domains:
            existing_domains.add(d)
            added.append(entry)

    if not added:
        print("[merge] nothing new to add")
        return

    # append with the same simple formatting the file already uses
    lines = SRC.read_text(encoding="utf-8").rstrip("\n").split("\n")
    for e in added:
        name = str(e["company"]).replace('"', "")
        lines.append(f'  - company: "{name}"')
        lines.append(f'    url: "{e["url"]}"')
    SRC.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # sanity: file must still parse
    parsed = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    total = len(parsed["sources"])

    with open(LOG, "a", encoding="utf-8") as f:
        for e in added:
            f.write(f"{date.today().isoformat()}  {e['company']}  {e['url']}\n")

    print(f"[merge] added {len(added)} new sources — total now {total}")


if __name__ == "__main__":
    main()
