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


def flatten_strings(obj, max_depth=4) -> str:
    """Recursively pull every string value out of a JSON-like structure (dicts,
    lists, nested combinations) and join them into one blob. Used so keyword
    matching sees full descriptions/topic text/etc, not just whichever single
    field we guessed was "the" title or objective -- API response shapes for
    these sources aren't guaranteed to be flat or consistently named."""
    if max_depth <= 0:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(flatten_strings(v, max_depth - 1) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(flatten_strings(v, max_depth - 1) for v in obj)
    return ""


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
        # Prefer an actual description-shaped field for the display objective;
        # fall back through a few plausible field names since the API's exact
        # schema for a given solicitation can vary.
        objective = (
            item.get("topic_description")
            or item.get("solicitation_description")
            or item.get("description")
            or ""
        )
        # For *matching*, don't rely on guessing the right field name -- pull
        # every string in the whole item (including nested topic lists, agency
        # names, keywords arrays, etc.) so nothing relevant gets missed.
        full_blob = flatten_strings(item)
        if not is_relevant(full_blob):
            continue
        close_date = item.get("close_date") or item.get("solicitation_close_date")
        open_date = item.get("open_date") or item.get("solicitation_open_date")
        url = item.get("sbir_solicitation_link") or "https://www.dodsbirsttr.mil/topics-app/"
        results.append({
            "id": f"sbir-api-{slugify(name)}",
            "name": name.strip(),
            "source": "DoD SBIR",
            "objective": objective.strip() or "See listing for full description.",
            "openDate": open_date,
            "closeDate": close_date,
            "cmmcRequirement": None,
            "fundingAmount": None,
            "applicationUrl": url,
            "notes": "Auto-matched by scan -- fit level not yet manually reviewed.",
            "products": keyword_matches(full_blob) or ["Review"],
            "fitLevel": "Review",
            "firstSeen": today_iso(),
        })
    return results


# Boilerplate link text that shows up on nearly every site's markup but is
# never an actual opportunity -- accessibility skip-links, nav labels, footer
# links, etc. Anything matching these (case-insensitively, after stripping
# punctuation) gets discarded before the keyword filter even runs.
BOILERPLATE_LINK_TEXT = {
    "skip to content", "skip to main content", "skip to navigation",
    "skip navigation", "skip to footer", "back to top", "home",
    "menu", "search", "login", "log in", "sign in", "contact",
    "contact us", "about", "about us", "privacy policy", "privacy",
    "terms of use", "terms of service", "accessibility",
    "accessibility statement", "sitemap", "site map", "careers",
    "newsletter", "subscribe", "facebook", "twitter", "linkedin",
    "instagram", "youtube", "read more", "learn more", "next", "previous",
    "close", "faq", "faqs", "help", "cookie policy", "cookies",
}


def is_boilerplate_link(title: str, href: str) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    if normalized in BOILERPLATE_LINK_TEXT:
        return True
    if href.strip().startswith("#"):
        return True  # same-page anchor, not a real destination
    if len(normalized) < 12:
        return True  # too short to plausibly be an opportunity title
    return False


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
        if not title or not href:
            continue
        if is_boilerplate_link(title, href):
            continue
        if title in seen_titles:
            continue
        # Pull surrounding context to improve matching -- descriptions for
        # these listings often live in a sibling element, not the link text
        # itself, so check the immediate parent block first and widen the
        # net to a grandparent if that block looks too thin to hold a real
        # description. Also fold in title/aria-label attributes, which some
        # sites use for a fuller description that isn't visible link text.
        attr_text = " ".join(filter(None, [link.get("title"), link.get("aria-label")]))
        context = link.find_parent(["li", "div", "article", "section"])
        context_text = context.get_text(" ", strip=True) if context else title
        if len(context_text) < 40 and context is not None:
            wider = context.find_parent(["li", "div", "article", "section"])
            if wider:
                context_text = wider.get_text(" ", strip=True)
        full_context = f"{context_text} {attr_text}"
        if not is_relevant(full_context):
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
            "products": keyword_matches(full_context) or ["Review"],
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
        full_blob = flatten_strings(item)
        if not is_relevant(full_blob):
            continue
        results.append({
            "id": f"samgov-{slugify(name)}",
            "name": name,
            "source": "SAM.gov",
            "objective": item.get("description") or "Auto-collected lead -- confirm details on SAM.gov.",
            "openDate": item.get("postedDate"),
            "closeDate": item.get("responseDeadLine"),
            "cmmcRequirement": None,
            "fundingAmount": None,
            "applicationUrl": item.get("uiLink", "https://sam.gov"),
            "notes": "Picked up by keyword scan; not yet manually reviewed.",
            "products": keyword_matches(full_blob) or ["Review"],
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


# How long to keep an opportunity visible after its close date passes,
# before the scanner removes it from opportunities.json entirely.
GRACE_PERIOD_DAYS = 7


def prune_expired(opportunities):
    """Drop opportunities whose close date is more than GRACE_PERIOD_DAYS in
    the past. Opportunities with no close date (TBD) are always kept, since
    there's nothing to measure the grace period against."""
    today = datetime.now(timezone.utc).date()
    kept, removed = [], []
    for o in opportunities:
        close_date_str = o.get("closeDate")
        if not close_date_str:
            kept.append(o)
            continue
        try:
            close_date = datetime.fromisoformat(close_date_str[:10]).date()
        except ValueError:
            # Unparseable date -- keep it rather than silently losing data,
            # but flag it so it's easy to spot and fix by hand.
            print(f"[prune] could not parse closeDate '{close_date_str}' for '{o.get('name')}', keeping it", file=sys.stderr)
            kept.append(o)
            continue
        days_past_close = (today - close_date).days
        if days_past_close > GRACE_PERIOD_DAYS:
            removed.append(o)
        else:
            kept.append(o)
    if removed:
        names = ", ".join(o.get("name", o.get("id", "?")) for o in removed)
        print(f"Pruned {len(removed)} expired opportunit{'y' if len(removed)==1 else 'ies'} (closed >{GRACE_PERIOD_DAYS}d ago): {names}")
    return kept


def main():
    existing = load_existing()
    new_items = []
    new_items += scan_sbir_api()
    new_items += scan_xtech()
    new_items += scan_diu()
    new_items += scan_afwerx()
    new_items += scan_sam_gov()

    merged = merge(existing, new_items)
    merged = prune_expired(merged)
    output = {"lastScan": datetime.now(timezone.utc).isoformat(), "opportunities": merged}

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
