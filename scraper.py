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
  ANTHROPIC_API_KEY  -> Claude-based IT-job classification + tech-stack extraction
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
    "Accept-Language":
