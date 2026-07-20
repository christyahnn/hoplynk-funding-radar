# Hoplynk Funding Radar

A small self-updating site that tracks government funding opportunities
(SBIR/DSIP, xTech, DIU, AFWERX, SAM.gov, NTIA) and highlights the ones
relevant to Hoplynk's products: **HAVEN**, **Argus**, **Hydra**, and **GoLynk**.

- `index.html` — the dashboard (dark theme, filterable by product, sorted by
  close date, badges new items since your last visit).
- `data/opportunities.json` — the data the dashboard reads. Starts seeded
  with the opportunities from your curated list.
- `scripts/hoplynk_scan.py` — pulls fresh opportunities from SBIR.gov's API
  and scrapes xTech/DIU/AFWERX/SAM.gov, keyword-matches them against
  Hoplynk's product profile, and merges new finds into `opportunities.json`
  without deleting or overwriting your manual notes.
- `.github/workflows/scan.yml` — runs the scanner once a day (and on demand)
  and commits any changes back to the repo.

## 1. Push this to GitHub

```bash
cd hoplynk-funding-radar
git init
git add .
git commit -m "Initial funding radar"
git branch -M main
git remote add origin https://github.com/<your-org>/hoplynk-funding-radar.git
git push -u origin main
```

## 2. Turn on GitHub Pages

In the repo: **Settings → Pages → Build and deployment → Source: Deploy from
a branch → Branch: `main` / root**. GitHub will give you a URL like
`https://<your-org>.github.io/hoplynk-funding-radar/`. Because the dashboard
just does `fetch('data/opportunities.json')`, every time the scan workflow
commits an update, the live site reflects it on next load — no separate
deploy step needed.

## 3. Turn on the scheduled scan

It's already wired up in `.github/workflows/scan.yml` — nothing to do
beyond having the repo on GitHub with Actions enabled (on by default). It
runs daily at 13:00 UTC and can also be triggered manually from the
**Actions** tab (`Scan funding sources → Run workflow`).

## 4. Optional: add API keys / notifications

Add these under **Settings → Secrets and variables → Actions** if you want
them:

- `SAM_API_KEY` — free key from [sam.gov/data-services](https://sam.gov/data-services). Without it, the SAM.gov source is skipped (everything else still runs).
- `SLACK_WEBHOOK_URL` — an [incoming webhook URL](https://api.slack.com/messaging/webhooks) for a Slack channel. When set, the workflow posts a message any time the scan finds new opportunities. Swap this step for an email step if you'd rather get email — happy to wire that up too.

## Tuning what counts as "relevant"

Open `scripts/hoplynk_scan.py` and edit `PRODUCT_KEYWORDS` (per-product terms)
and `GENERAL_KEYWORDS` (broader terms that still warrant a second look). The
scanner tags every auto-collected opportunity with `"fitLevel": "Review"` —
it's deliberately permissive, since a false positive costs you 10 seconds of
reading and a false negative costs you a missed opportunity. Once you've
reviewed one, edit its `fitLevel`, `products`, and `notes` fields directly in
`data/opportunities.json`; the scanner won't overwrite those fields on
future runs, only `openDate`/`closeDate`/`applicationUrl` get refreshed.

## Known limitations to know about up front

- **xTech, DIU, and AFWERX don't have public APIs**, so those three are
  scraped from their public listing pages. Scraped results are treated as
  *leads* (`fitLevel: "Review"`, generic objective text) rather than final
  entries — the page markup can also change without notice, which would
  silently zero out that source until the selector in `scan_html_listing()`
  is updated to match the new page.
- **SAM.gov requires a free API key** (see above); it's skipped otherwise.
- **OCED (DOE)** wasn't included as an automated source since, per your
  notes, it posts new opportunities rarely — worth an occasional manual
  check of <https://oced-exchange.energy.gov/> instead of a daily scan.
- I wrote and tested this scanner's logic carefully, but couldn't actually
  execute it against the live SBIR.gov API or scrape the live xTech/DIU/AFWERX
  pages from this environment (no network access here). Before relying on
  it, run `python scripts/hoplynk_scan.py` locally once and check that
  `data/opportunities.json` picks up sensible results — if a source's HTML
  structure doesn't match what `scan_html_listing()` expects, it'll just
  return zero results for that source rather than erroring, so it's worth
  eyeballing the output the first time.
