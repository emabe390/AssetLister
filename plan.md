# AssetLister — Implementation Plan

## Goal

A website that lists all packaged ship hulls owned by a single character at
**Dal 6** (planet VI in the Dal solar system, Genesis region) in EVE Online.
> Note: exact station/structure ID to be confirmed in Phase 2.

- Static site, hosted on **GitHub Pages**
- Data fetched from **EVE ESI API** using an ESI key (access token)
- Refreshed by a **Python script** that runs periodically, regenerates the
  site data, and commits changes back to the git repository

## Architecture Overview

```
┌─────────────┐    ┌──────────────────┐    ┌────────────────┐
│  EVE ESI    │───▶│ Python updater   │───▶│  Git repo      │
│  (assets)   │    │ (fetch + render) │    │  (static site) │
└─────────────┘    └──────────────────┘    └───────┬────────┘
                                                   │ push
                                            ┌──────▼────────┐
                                            │  GitHub Pages  │
                                            │  (public site) │
                                            └────────────────┘
```

## Components

### 1. Python updater script (`update.py`)

- **Input:** ESI refresh/access token (environment variable or config file,
  kept out of the repo — `.gitignore`d)
- **Steps:**
  1. Authenticate against ESI (`oauth2` token, scope:
     `esi-assets.read_assets.v1`)
  2. Fetch the character's assets (paginated `GET /v5/characters/{id}/assets/`)
  3. Filter items:
     - Location: **Dal 6** — resolve the exact station/structure ID
       (e.g. via station/structure endpoints) once confirmed
     - Item: **packaged ship hulls** (`is_singleton: false`) whose type
       belongs to ship hull categories (Ship, capitals, etc.)
  4. Group/aggregate hulls by type, sum quantities
  5. Resolve type names via `POST /v3/universe/names/`
  6. Render static output (`data.json` + HTML, or just JSON consumed by a
     small JS front-end)
  7. If anything changed, `git commit` + `git push`

### 2. Static site (`docs/` or root)

- `index.html` — simple page: table of ship hulls, quantities, total value
  (optional)
- Loads `data.json` and renders client-side (no build tooling needed)
- Styled minimally (plain CSS or a tiny framework via CDN)

### 3. Automation

- **Option A (recommended):** GitHub Actions workflow on a schedule
  (cron, e.g. every hour) running `py update.py`, committing and pushing
  changes
- ESI refresh token stored as a GitHub Actions **secret**
- **Option B (local):** Windows Task Scheduler running the script locally

## Phases

### Phase 1 — Skeleton
- [ ] `git init`, this repo, add `idea.md` + `plan.md`
- [ ] `.gitignore` (config with tokens, `__pycache__/`)
- [ ] Repo pushed to GitHub, GitHub Pages enabled

### Phase 2 — ESI data fetch
- [ ] Register an ESI app (developer.eveonline.com) with
      `esi-assets.read_assets.v1` scope
- [ ] Token handling: initial auth flow (SSO) → refresh token
- [ ] Fetch + paginate assets, cache responses locally
- [ ] Filter: location = Dal 6, packaged ship hulls only
- [ ] Name resolution (type names, structure/station names)

### Phase 3 — Static site
- [ ] `index.html` + `data.json` output
- [ ] Table: hull name, quantity, (optional) market value via
      `esi-markets.*` prices endpoint
- [ ] Basic responsive styling

### Phase 4 — Automation
- [ ] GitHub Actions workflow: scheduled run of `update.py`
- [ ] Auto-commit and push on data changes
- [ ] Secret management for the refresh token

## Open Questions

- Character ID / name to track (needed for the ESI calls)
- Which ship hull categories count (all ships incl. caps/supers, or subcaps
  only?)
- Show estimated market value, or just quantities?
- Update frequency (hourly? daily?)

## Tech Notes

- EVE ESI base URL: `https://esi.evetech.net/latest`
- Python via `py` launcher (Windows machine — see AGENTS.md); use
  `py -m pip install requests` for dependencies
- Keep dependencies minimal: `requests` only, if possible
- ESI requires a `User-Agent` header identifying the app
