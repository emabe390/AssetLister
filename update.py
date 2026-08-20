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
JANICE_RPC = "https://janice.e-351.com/api/rpc/v1"
JANICE_JITA_MARKET_ID = 2  # "Jita 4-4"
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


def fetch_janice_prices(hulls, names):
    """Price all hulls via a single Janice appraisal (Jita 4-4 market).

    Returns {type_id: unit_sell_price}. Uses the 'effective' sell price,
    the price for selling the whole stack.
    """
    # Janice paste format: "<name> x<qty>" per line
    lines = [f"{names.get(tid, tid)} x{qty}" for tid, qty in hulls.items()]
    params = {
        "marketId": JANICE_JITA_MARKET_ID,
        "designation": 1,
        "pricing": 1,        # effective prices
        "pricingVariant": 1,
        "pricePercentage": 1.0,  # multiplier, NOT percent (100 would 100x the price!)
        "input": "\n".join(lines),
        "comment": "",
        "compactize": False,
    }
    body = json.dumps({"method": "Appraisal.create", "params": params, "id": 1}).encode()
    req = urllib.request.Request(
        JANICE_RPC,
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())["result"]

    prices = {}
    for item in result.get("items", []):
        tid = item["itemType_eid"]
        # sellPrice is per-unit; sellPriceTotal is for the whole stack
        unit_price = item.get("effectivePrices", {}).get("sellPrice", 0)
        if unit_price:
            prices[tid] = unit_price
    if result.get("failures"):
        print(f"  warning: janice failures: {result['failures'][:200]}")
    return prices


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

    # Janice sell prices (Jita 4-4, whole-stack effective prices, one API call)
    print("Fetching Janice prices (Jita 4-4)...")
    try:
        prices = fetch_janice_prices(hulls, names)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"  warning: Janice pricing failed ({e}); continuing without prices")
        prices = {}
    priced = sum(1 for p in prices.values() if p)
    print(f"  {priced}/{len(hulls)} types priced")

    # Build data.json
    rows = [
        {
            "type_id": tid,
            "name": names.get(tid, f"type {tid}"),
            "quantity": qty,
            "jita_sell": prices.get(tid) or None,
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
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 74rem;
         background: #111; color: #ddd; padding: 0 1rem; }
  h1 { color: #e8e2cf; }
  h2 { color: #e8e2cf; font-size: 1.1rem; margin: 0 0 0.8rem; }
  .meta { color: #888; margin-bottom: 1.5rem; }
  .updated { color: #c9a; }
  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 24rem; gap: 1.5rem;
            align-items: start; }
  @media (max-width: 64rem) { .layout { grid-template-columns: 1fr; } }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; }
  th { color: #c9a; }
  th.qty, th.price, td.qty, td.price { text-align: right; }
  td.price { color: #b6c7a6; white-space: nowrap; }
  tr:hover { background: #1c1c1c; }
  .icon { width: 32px; height: 32px; vertical-align: middle; margin-right: 0.6rem; }
  .hullname { display: inline-flex; align-items: center; gap: 0.6rem; }
  .total { margin-top: 1rem; font-weight: bold; }
  .buy { display: inline-flex; gap: 0.35rem; justify-content: flex-end; flex-wrap: wrap; }
  button { background: #2a2a2a; color: #ddd; border: 1px solid #444; border-radius: 4px;
           padding: 0.25rem 0.6rem; cursor: pointer; font-size: 0.85rem; }
  button:hover { background: #3a3a3a; border-color: #c9a; }
  .panel { background: #1a1a1a; border: 1px solid #333; border-radius: 6px;
           padding: 1rem; position: sticky; top: 1rem; }
  .orderItem { display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0;
               border-bottom: 1px solid #2a2a2a; }
  .orderItem .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                     white-space: nowrap; }
  .orderItem input { width: 4.4rem; background: #111; color: #ddd; border: 1px solid #444;
                     border-radius: 4px; padding: 0.2rem 0.3rem; text-align: right; }
  .orderItem .linePrice { color: #b6c7a6; white-space: nowrap; font-size: 0.85rem;
                          min-width: 5.5rem; text-align: right; }
  .orderItem .remove { color: #a66; border: none; background: none; font-size: 1rem;
                       padding: 0.1rem 0.3rem; }
  .orderItem .remove:hover { color: #f88; background: none; border: none; }
  .empty { color: #777; font-style: italic; }
  .orderTotal { font-weight: bold; margin: 0.8rem 0; }
  textarea { width: 100%; box-sizing: border-box; background: #111; color: #bbb;
             border: 1px solid #444; border-radius: 4px; padding: 0.5rem;
             font-family: ui-monospace, monospace; font-size: 0.8rem; resize: vertical; }
  .copyBtn { width: 100%; margin-top: 0.6rem; padding: 0.5rem; font-size: 0.95rem; }
</style>
</head>
<body>
<h1>Packaged Ship Hulls</h1>
<p class="meta" id="meta">Loading…</p>
<p class="meta">Last updated: <span class="updated" id="updated"></span></p>
<div class="layout">
  <div>
    <table>
      <thead><tr><th>Hull</th><th class="qty">In Stock</th><th class="price">Unit Price</th><th></th></tr></thead>
<!-- prices via Janice (janice.e-351.com), Jita 4-4 market -->
      <tbody id="rows"></tbody>
    </table>
    <p class="total" id="total"></p>
  </div>
  <aside class="panel">
    <h2>Your Order</h2>
    <div id="orderItems"><p class="empty">Use the Buy buttons to add hulls to your order.</p></div>
    <p class="orderTotal" id="orderTotal"></p>
    <textarea id="orderText" rows="12" readonly spellcheck="false"
      placeholder="Your EVEmail-ready order will appear here…"></textarea>
    <button class="copyBtn" id="copyBtn">Copy order for EVEmail</button>
  </aside>
</div>
<script>
const CART_KEY = 'assetlister_cart_v1';
let DATA = null;
let hullById = {};
let cart = {};
try { cart = JSON.parse(localStorage.getItem(CART_KEY)) || {}; } catch (e) { cart = {}; }

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
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

function saveCart() {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
}

function clampCartToStock() {
  // Stock changes between visits — drop or trim anything no longer available.
  for (const tid of Object.keys(cart)) {
    const hull = hullById[tid];
    if (!hull || hull.quantity <= 0 || cart[tid] <= 0) delete cart[tid];
    else if (cart[tid] > hull.quantity) cart[tid] = hull.quantity;
  }
}

function addToCart(tid, n) {
  const hull = hullById[tid];
  if (!hull) return;
  const qty = Math.min((cart[tid] || 0) + n, hull.quantity);
  if (qty > 0) cart[tid] = qty;
  render();
}

function setCartQty(tid, qty) {
  const hull = hullById[tid];
  if (!hull) return;
  qty = Math.max(0, Math.min(Math.floor(qty) || 0, hull.quantity));
  if (qty > 0) cart[tid] = qty; else delete cart[tid];
  render();
}

function removeFromCart(tid) {
  delete cart[tid];
  render();
}

function orderEntries() {
  return Object.keys(cart)
    .map(tid => hullById[tid] ? { tid, hull: hullById[tid], qty: cart[tid] } : null)
    .filter(Boolean)
    .sort((a, b) => a.hull.name.localeCompare(b.hull.name));
}

function orderTotalValue() {
  return orderEntries().reduce((sum, e) =>
    sum + (e.hull.jita_sell ? e.hull.jita_sell * e.qty : 0), 0);
}

function orderText() {
  const entries = orderEntries();
  if (!entries.length) return '';
  const lines = entries.map(e => `${e.qty}x ${e.hull.name}`);
  return `Hi ${DATA.character},\n\n` +
    `I'd like to buy the following hulls from your stock at ${DATA.location}:\n\n` +
    lines.join('\n') +
    `\n\nEstimated total: ${isk(orderTotalValue())}\n\nThanks!`;
}

function render() {
  saveCart();
  const entries = orderEntries();
  const itemsEl = document.getElementById('orderItems');
  if (!entries.length) {
    itemsEl.innerHTML = '<p class="empty">Use the Buy buttons to add hulls to your order.</p>';
  } else {
    itemsEl.innerHTML = entries.map(e => {
      const line = e.hull.jita_sell ? isk(e.hull.jita_sell * e.qty) : '—';
      return `<div class="orderItem">` +
        `<span class="name" title="${esc(e.hull.name)}">${esc(e.hull.name)}</span>` +
        `<input type="number" min="1" max="${e.hull.quantity}" value="${e.qty}" data-tid="${e.tid}">` +
        `<span class="linePrice">${line}</span>` +
        `<button class="remove" data-remove="${e.tid}" title="Remove">✕</button>` +
        `</div>`;
    }).join('');
  }
  document.getElementById('orderTotal').textContent = entries.length
    ? `${entries.length} item type${entries.length > 1 ? 's' : ''} — est. ${isk(orderTotalValue())}`
    : '';
  document.getElementById('orderText').value = orderText();
}

// Buy buttons in the hull table
document.getElementById('rows').addEventListener('click', ev => {
  const btn = ev.target.closest('button[data-buy]');
  if (!btn) return;
  const tid = btn.dataset.tid;
  if (btn.dataset.buy === 'max') {
    const hull = hullById[tid];
    if (hull) setCartQty(tid, hull.quantity);
  } else {
    addToCart(tid, parseInt(btn.dataset.buy, 10));
  }
});

// Quantity edits + removal in the order panel
document.getElementById('orderItems').addEventListener('change', ev => {
  const input = ev.target.closest('input[data-tid]');
  if (input) setCartQty(input.dataset.tid, parseInt(input.value, 10));
});
document.getElementById('orderItems').addEventListener('click', ev => {
  const btn = ev.target.closest('button[data-remove]');
  if (btn) removeFromCart(btn.dataset.remove);
});

// Copy-to-clipboard for the EVEmail
document.getElementById('copyBtn').addEventListener('click', async () => {
  const ta = document.getElementById('orderText');
  if (!ta.value) return;
  let ok = false;
  try { await navigator.clipboard.writeText(ta.value); ok = true; }
  catch (e) {
    ta.select();
    ok = document.execCommand('copy');
  }
  const btn = document.getElementById('copyBtn');
  btn.textContent = ok ? 'Copied! ✓' : 'Copy failed — select the text manually';
  setTimeout(() => { btn.textContent = 'Copy order for EVEmail'; }, 2000);
});

fetch('data.json').then(r => r.json()).then(d => {
  DATA = d;
  for (const h of d.hulls) hullById[h.type_id] = h;
  clampCartToStock();

  document.getElementById('meta').textContent =
    `${d.character} — ${d.location}`;
  document.getElementById('rows').innerHTML = d.hulls
    .map(h => `<tr><td><span class="hullname">` +
      `<img class="icon" loading="lazy" alt="" ` +
      `src="https://images.evetech.net/types/${h.type_id}/icon?size=32">` +
      `${esc(h.name)}</span></td><td class="qty">${h.quantity}</td>` +
      `<td class="price">${isk(h.jita_sell)}</td>` +
      `<td><span class="buy">` +
      `<button data-buy="1" data-tid="${h.type_id}">Buy 1</button>` +
      `<button data-buy="10" data-tid="${h.type_id}">Buy 10</button>` +
      `<button data-buy="max" data-tid="${h.type_id}">Max (${h.quantity})</button>` +
      `</span></td></tr>`).join('');
  document.getElementById('total').textContent =
    `${d.total_hulls} hulls total (${d.distinct_types} types) — est. value ${isk(d.total_value)}`;
  const upd = document.getElementById('updated');
  const tick = () => {
    upd.textContent = `${new Date(d.updated).toLocaleString()} — ${timeAgo(d.updated)}`;
  };
  tick();
  setInterval(tick, 1000);
  render();
}).catch(e => document.getElementById('meta').textContent = 'Failed to load data');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
