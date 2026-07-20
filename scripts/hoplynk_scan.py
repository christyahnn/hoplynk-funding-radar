#!/usr/bin/env python3
"""
Hoplynk Funding Radar scanner.

Pulls open opportunities from a handful of government sources, filters them
against Hoplynk's product profile (HAVEN, Argus, Hydra, GoLynk), and merges
new/updated results into data/opportunities.json. Existing manually-curated
entries (or ones already in the file) are never deleted -- only added to or
refreshed -- so hand-written notes and fit assessments survive re-runs.

Run locally:
    pip install requests beautifulsoup4
    python scripts/hoplynk_scan.py

In CI (see .github/workflows/scan.yml) this runs on a schedule and commits
any changes to data/opportunities.json back to the repo.

Notes on sources:
  - SBIR.gov / DSIP has a public JSON API, queried directly below.
  - xTech, DIU, and AFWERX don't have stable public APIs, so those are
    scraped from their public listing pages with BeautifulSoup. Their page
    markup can change without notice -- if a source stops returning
    results, check SOURCE_URLS below against the live site and adjust the
    selectors in the matching `scan_*` function.
  - SAM.gov requires a (free) API key: https://sam.gov/data-services/
    Set the SAM_API_KEY environment variable / repo secret to enable it;
    the script skips SAM.gov silently if it isn't set.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "opportunities.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HoplynkFundingRadar/1.0)"}

SOURCE_URLS = {
    "sbir_api": "https://api.www.sbir.gov/public/api/solicitations",
    "xtech": "https://xtech.army.mil/competitions/",
    "diu": "https://www.diu.mil/work-with-us/open-solicitations",
    "afwerx": "https://afwerx.com/challenges/",
    "sam_api": "https://api.sam.gov/opportunities/v2/search",
}

# ---------------------------------------------------------------------------
# Hoplynk product profile -- edit this to tune what counts as "relevant".
# ---------------------------------------------------------------------------

PRODUCT_KEYWORDS = {
    "HAVEN": [
        "multi-transport", "transport aggregation", "network aggregat",
        "contested environment", "denied environment", "disconnected",
        "intermittent", "limited bandwidth", "dil", "mesh network",
        "satcom", "satellite communications", "rf communications",
        "resilient communications", "resilient network", "edge network",
        "tactical data link", "beyond line of sight", "line of sight comms",
    ],
    "Argus": [
        "fleet management", "fleet control", "control plane",
        "device management", "node management", "telemetry",
        "situational awareness", "network monitoring", "sensor tasking",
        "resource allocation", "swarm coordination", "swarm management",
    ],
    "Hydra": [
        "autonomous network", "policy-driven", "self-healing network",
        "software defined network", "sdn", "zero trust", "ai-native",
        "network automation", "autonomous policy", "network execution",
        "decentralized control", "adaptive routing",
    ],
    "GoLynk": [
        "rapid deploy", "rapid deployment", "expeditionary",
        "quick deploy", "commercial networking", "field deployable",
        "portable network", "man-packable",
    ],
}

# Broader terms that make a topic worth a second look even without a
# precise product-keyword hit.
GENERAL_KEYWORDS = [
    "network", "networking", "connectivity", "communications", "comms",
    "drone", "uas", "unmanned", "radar", "sensor fusion", "command and control",
    "c2", "multi-domain", "autonomous", "ai agent", "artificial intelligence",
    "resilient", "resilience", "edge computing", "wireless",
]


def keyword_matches(text: str):
    """Return the list of Hoplynk products a piece of text seems relevant to."""
    text_l = text.lower()
    matched = []
    for product, keywords in PRODUCT_KEYWORDS.items():
        if any(kw in text_l for kw in keywords):
            matched.append(product)
    return matched


def is_relevant(text: str) -> bool:
    text_l = text.lower()
    if keyword_matches(text):
        return True
    return any(kw in text_l for kw in GENERAL_KEYWORDS)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80]


def today_iso():
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Source scanners -- each returns a list of opportunity dicts in the same
# shape used by data/opportunities.json.
# ---------------------------------------------------------------------------

def scan_sbir_api():
    """Query the public SBIR.gov / DSIP API for open DoD solicitations."""
    results = []
    try:
        resp = requests.get(
            SOURCE_URLS["sbir_api"],
            params={"agency": "DOD", "open": 1, "rows": 100},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[sbir_api] fetch failed: {e}", file=sys.stderr)
        return results

    for item in data if isinstance(data, list) else data.get("results", []):
        name = item.get("solicitation_title") or item.get("topic_title") or ""
        objective = item.get("solicitation_agency") or item.get("description") or ""
        blob = f"{name} {objective}"
        if not is_relevant(blob):
            continue
        close_date = item.get("close_date") or item.get("solicitation_close_date")
        open_date = item.get("open_date") or item.get("solicitation_open_date")
        url = item.get("sbir_solicitation_link") or "https://www.dodsbirsttr.mil/topics-app/"
        results.append({
            "id": f"sbir-api-{slugify(name)}",
            "name": name.strip(),
            "source": "DoD SBIR",
            "objective": objective.strip(),
            "openDate": open_date,
            "closeDate": close_date,
            "cmmcRequirement": None,
            "fundingAmount": None,
            "applicationUrl": url,
            "notes": "Auto-matched by scan -- fit level not yet manually reviewed.",
            "products": keyword_matches(blob) or ["Review"],
            "fitLevel": "Review",
            "firstSeen": today_iso(),
        })
    return results


def scan_html_listing(source_name, url, link_selector="a"):
    """Generic scraper: grab links + surrounding text from a listing page and
    keep anything that matches Hoplynk's keyword profile. This is intentionally
    broad since these sites don't expose structured data -- treat results as
    leads to confirm manually, not final entries."""
    results = []
    if BeautifulSoup is None:
        print(f"[{source_name}] beautifulsoup4 not installed, skipping", file=sys.stderr)
        return results
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[{source_name}] fetch failed: {e}", file=sys.stderr)
        return results

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_titles = set()
    for link in soup.select(link_selector):
        title = link.get_text(strip=True)
        href = link.get("href")
        if not title or not href or len(title) < 8:
            continue
        if title in seen_titles:
            continue
        # Pull a little surrounding context (parent block text) to improve matching
        context = link.find_parent(["li", "div", "article", "section"])
        context_text = context.get_text(" ", strip=True) if context else title
        if not is_relevant(context_text):
            continue
        seen_titles.add(title)
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        results.append({
            "id": f"{source_name}-{slugify(title)}",
            "name": title,
            "source": source_name,
            "objective": "Auto-collected lead -- confirm details on the source page.",
            "openDate": None,
            "closeDate": None,
            "cmmcRequirement": None,
            "fundingAmount": None,
            "applicationUrl": href,
            "notes": "Picked up by keyword scan; not yet manually reviewed.",
            "products": keyword_matches(context_text) or ["Review"],
            "fitLevel": "Review",
            "firstSeen": today_iso(),
        })
    return results


def scan_xtech():
    return scan_html_listing("xTech", SOURCE_URLS["xtech"], "a")


def scan_diu():
    return scan_html_listing("DIU", SOURCE_URLS["diu"], "a")


def scan_afwerx():
    return scan_html_listing("AFWERX", SOURCE_URLS["afwerx"], "a")


def scan_sam_gov():
    """Requires a SAM.gov API key (free, self-service at sam.gov/data-services).
    Skips quietly if SAM_API_KEY isn't set."""
    api_key = os.environ.get("SAM_API_KEY")
    if not api_key:
        print("[sam_gov] SAM_API_KEY not set, skipping", file=sys.stderr)
        return []
    results = []
    try:
        resp = requests.get(
            SOURCE_URLS["sam_api"],
            params={
                "api_key": api_key,
                "postedFrom": datetime.now().strftime("01/01/%Y"),
                "postedTo": datetime.now().strftime("%m/%d/%Y"),
                "ptype": "o",  # solicitations
                "limit": 100,
                "title": "network",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[sam_gov] fetch failed: {e}", file=sys.stderr)
        return results

    for item in data.get("opportunitiesData", []):
        name = item.get("title", "")
        blob = f"{name} {item.get('description', '')}"
        if not is_relevant(blob):
            continue
        results.append({
            "id": f"samgov-{slugify(name)}",
            "name": name,
            "source": "SAM.gov",
            "objective": "Auto-collected lead -- confirm details on SAM.gov.",
            "openDate": item.get("postedDate"),
            "closeDate": item.get("responseDeadLine"),
            "cmmcRequirement": None,
            "fundingAmount": None,
            "applicationUrl": item.get("uiLink", "https://sam.gov"),
            "notes": "Picked up by keyword scan; not yet manually reviewed.",
            "products": keyword_matches(blob) or ["Review"],
            "fitLevel": "Review",
            "firstSeen": today_iso(),
        })
    return results


# ---------------------------------------------------------------------------
# Merge + write
# ---------------------------------------------------------------------------

def load_existing():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"lastScan": None, "opportunities": []}


def merge(existing_data, new_items):
    existing_by_id = {o["id"]: o for o in existing_data.get("opportunities", [])}
    added, updated = 0, 0
    for item in new_items:
        if item["id"] in existing_by_id:
            # Refresh dates/link but keep any hand-edited notes/fitLevel/products
            existing = existing_by_id[item["id"]]
            for field in ("openDate", "closeDate", "applicationUrl"):
                if item.get(field):
                    existing[field] = item[field]
            updated += 1
        else:
            existing_by_id[item["id"]] = item
            added += 1
    merged = list(existing_by_id.values())
    print(f"Merge complete: {added} new, {updated} refreshed, {len(merged)} total")
    return merged


def main():
    existing = load_existing()
    new_items = []
    new_items += scan_sbir_api()
    new_items += scan_xtech()
    new_items += scan_diu()
    new_items += scan_afwerx()
    new_items += scan_sam_gov()

    merged = merge(existing, new_items)
    output = {"lastScan": datetime.now(timezone.utc).isoformat(), "opportunities": merged}

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
