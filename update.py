"""AssetLister updater.

Fetches Kalder Okanata's assets from EVE ESI, filters for packaged ship
hulls at the configured station, writes the static site data (docs/),
and commits + pushes changes if the data changed.

Run:  py update.py
"""

import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

CONFIG_FILE = "config.json"
TOKENS_FILE = "tokens.json"
CACHE_DIR = Path("cache")
DOCS_DIR = Path("docs")

ESI = "https://esi.evetech.net"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

SHIP_CATEGORY_ID = 6  # category "Ship"
JITA_STATION_ID = 60003760  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
THE_FORGE_REGION_ID = 10000002
STATION_ID = None  # filled from config at runtime

UA = "AssetLister github.com/emabe390/AssetLister"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def http_json(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get_access_token(cfg, tokens):
    """Use the refresh token to mint a fresh access token."""
    client_id = cfg["client_id"]
    client_secret = cfg.get("client_secret", "")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()
    resp = http_json(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
        },
        method="POST",
    )
    tokens["access_token"] = resp["access_token"]
    if "refresh_token" in resp:  # ESI may rotate refresh tokens
        tokens["refresh_token"] = resp["refresh_token"]
    save_json(TOKENS_FILE, tokens)
    return resp["access_token"]


def fetch_assets(character_id, access_token):
    """Fetch all asset pages for the character."""
    url = f"{ESI}/v5/characters/{character_id}/assets/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": UA,
        "Accept": "application/json",
    }
    items = []
    page = 1
    while True:
        req = urllib.request.Request(f"{url}?page={page}", headers=headers)
        with urllib.request.urlopen(req) as r:
            total_pages = int(r.headers.get("x-pages", 1))
            items.extend(json.loads(r.read()))
        if page >= total_pages:
            break
        page += 1
    return items


def esi_get(path):
    """Unauthenticated public ESI GET with local file cache."""
    cache_file = CACHE_DIR / (path.strip("/").replace("/", "_") + ".json")
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    data = http_json(f"{ESI}{path}", headers={"User-Agent": UA})
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


def esi_post_names(ids):
    """POST /v1/universe/names/ in chunks of 1000; returns id -> name."""
    result = {}
    ids = list(ids)
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        body = json.dumps(chunk).encode()
        req = urllib.request.Request(
            f"{ESI}/v3/universe/names/",
            data=body,
            headers={"User-Agent": UA, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            for item in json.loads(r.read()):
                result[item["id"]] = item["name"]
    return result


def fetch_jita_sell_price(type_id):
    """Cheapest sell order for a type at Jita 4-4, or None if none exist."""
    url = f"{ESI}/v1/markets/{THE_FORGE_REGION_ID}/orders/?type_id={type_id}&order_type=sell"
    page = 1
    best = None
    while True:
        req = urllib.request.Request(f"{url}&page={page}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req) as r:
            total_pages = int(r.headers.get("x-pages", 1))
            for o in json.loads(r.read()):
                if o["location_id"] == JITA_STATION_ID:
                    p = o["price"]
                    if best is None or p < best:
                        best = p
        if page >= total_pages:
            break
        page += 1
    return best


def is_ship_hull(type_id):
    """True if the type belongs to category 6 (Ship). Cached type/group lookups."""
    type_info = esi_get(f"/v3/universe/types/{type_id}/")
    group_id = type_info["group_id"]
    group_info = esi_get(f"/v1/universe/groups/{group_id}/")
    return group_info["category_id"] == SHIP_CATEGORY_ID


def git(*args):
    r = subprocess.run(
        ["git", *args], capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r


def main():
    cfg = load_json(CONFIG_FILE)
    tokens = load_json(TOKENS_FILE)
    character_id = cfg["character_id"]
    station_id = cfg["location"]["station_id"]

    print(f"Refreshing access token for {cfg['character_name']}...")
    access_token = get_access_token(cfg, tokens)

    print("Fetching assets (all pages)...")
    assets = fetch_assets(character_id, access_token)
    print(f"  {len(assets)} total asset rows")

    # Filter: packaged items at the target station
    candidates = [
        a for a in assets
        if not a.get("is_singleton", False) and a.get("location_id") == station_id
    ]
    print(f"  {len(candidates)} packaged rows at station {station_id}")

    # Aggregate quantity per type
    quantities = {}
    for a in candidates:
        quantities[a["type_id"]] = quantities.get(a["type_id"], 0) + a["quantity"]

    # Keep only ship hulls
    hulls = {}
    for type_id, qty in quantities.items():
        try:
            if is_ship_hull(type_id):
                hulls[type_id] = qty
        except urllib.error.HTTPError as e:
            print(f"  warning: type {type_id} lookup failed: {e.code}")

    print(f"  {len(hulls)} distinct ship hull types")

    # Resolve names
    names = esi_post_names(list(hulls.keys()))

    # Jita 4-4 sell prices (cheapest sell order per type)
    print("Fetching Jita sell prices...")
    prices = {}
    for tid in hulls:
        try:
            prices[tid] = fetch_jita_sell_price(tid)
        except urllib.error.HTTPError as e:
            print(f"  warning: market orders for type {tid} failed: {e.code}")
        time.sleep(0.1)  # stay polite with ESI rate limits
    priced = sum(1 for p in prices.values() if p is not None)
    print(f"  {priced}/{len(hulls)} types have sell orders at Jita 4-4")

    # Build data.json
    rows = [
        {
            "type_id": tid,
            "name": names.get(tid, f"type {tid}"),
            "quantity": qty,
            "jita_sell": prices.get(tid),
        }
        for tid, qty in sorted(hulls.items(), key=lambda kv: -kv[1])
    ]
    total_value = sum(
        p * hulls[tid] for tid, p in prices.items() if p is not None
    )
    data = {
        "character": cfg["character_name"],
        "location": cfg["location"]["name"],
        "total_hulls": sum(hulls.values()),
        "distinct_types": len(hulls),
        "total_value": total_value,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hulls": rows,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    data_file = DOCS_DIR / "data.json"
    old = data_file.read_text(encoding="utf-8") if data_file.exists() else None
    new = json.dumps(data, indent=4)
    data_file.write_text(new, encoding="utf-8")

    # Always write index.html so template updates propagate
    index_file = DOCS_DIR / "index.html"
    old_index = index_file.read_text(encoding="utf-8") if index_file.exists() else None
    index_file.write_text(INDEX_HTML, encoding="utf-8")

    if old == new and old_index == INDEX_HTML:
        print("No changes since last run.")
        return

    print("Data changed — committing and pushing...")
    git("add", "docs/")
    git("commit", "-m", f"Update asset data {data['updated']}")
    git("push")
    print("Done.")


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AssetLister — Kalder Okanata</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem;
         background: #111; color: #ddd; }
  h1 { color: #e8e2cf; }
  .meta { color: #888; margin-bottom: 1.5rem; }
  .updated { color: #c9a; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; }
  th { color: #c9a; }
  td.qty { text-align: right; }
  th.qty, th.price { text-align: right; }
  td.price { text-align: right; color: #b6c7a6; white-space: nowrap; }
  tr:hover { background: #1c1c1c; }
  .icon { width: 32px; height: 32px; vertical-align: middle; margin-right: 0.6rem; }
  .hullname { display: inline-flex; align-items: center; gap: 0.6rem; }
  .total { margin-top: 1rem; font-weight: bold; }
</style>
</head>
<body>
<h1>Packaged Ship Hulls</h1>
<p class="meta" id="meta">Loading…</p>
<p class="meta">Last updated: <span class="updated" id="updated"></span></p>
<table>
  <thead><tr><th>Hull</th><th class="qty">Qty</th><th class="price">Jita Sell</th><th class="price">Line Value</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<p class="total" id="total"></p>
<script>
function timeAgo(iso) {
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 5) return 'just now';
  const units = [['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1]];
  for (const [name, secs] of units) {
    if (s >= secs) {
      const v = Math.floor(s / secs);
      return `${v} ${name}${v > 1 ? 's' : ''} ago`;
    }
  }
}
function isk(n) {
  if (n === null || n === undefined) return '—';
  if (n >= 1e12) return (n / 1e12).toFixed(2) + 'T ISK';
  if (n >= 1e9)  return (n / 1e9).toFixed(2) + 'B ISK';
  if (n >= 1e6)  return (n / 1e6).toFixed(2) + 'M ISK';
  if (n >= 1e3)  return (n / 1e3).toFixed(1) + 'K ISK';
  return n.toFixed(0) + ' ISK';
}
fetch('data.json').then(r => r.json()).then(d => {
  document.getElementById('meta').textContent =
    `${d.character} — ${d.location}`;
  document.getElementById('rows').innerHTML = d.hulls
    .map(h => {
      const value = h.jita_sell !== null ? h.jita_sell * h.quantity : null;
      return `<tr><td><span class="hullname">` +
        `<img class="icon" loading="lazy" alt="" ` +
        `src="https://images.evetech.net/types/${h.type_id}/icon?size=32">` +
        `${h.name}</span></td><td class="qty">${h.quantity}</td>` +
        `<td class="price">${isk(h.jita_sell)}</td>` +
        `<td class="price">${isk(value)}</td></tr>`;
    }).join('');
  document.getElementById('total').textContent =
    `${d.total_hulls} hulls total (${d.distinct_types} types) — est. value ${isk(d.total_value)}`;
  const upd = document.getElementById('updated');
  const tick = () => {
    upd.textContent = `${new Date(d.updated).toLocaleString()} — ${timeAgo(d.updated)}`;
  };
  tick();
  setInterval(tick, 1000);
}).catch(e => document.getElementById('meta').textContent = 'Failed to load data');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
