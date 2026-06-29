"""bikes.kardol.us — the hub front door for the per-city bikeshare trackers.

Server-side aggregates each city's live "bikes available now" + station count by fetching
its in-cluster /api/now?neighborhood=everywhere (no CORS needed), and renders a flightdeck-
styled directory. Cities that are unreachable degrade to "—". Auto-refreshes every 60s.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

_OG = (Path(__file__).parent / "og.png").read_bytes()

CITIES = [
    {"slug": "nyc", "brand": "Citi Bike", "city": "New York + Jersey", "flag": "🗽",
     "domain": "citi.kardol.us", "svc": "http://citibike-web.citibike.svc.cluster.local:8000"},
    {"slug": "dc", "brand": "CaBi", "city": "Washington, DC", "flag": "🏛️",
     "domain": "cabi.kardol.us", "svc": "http://cabi-web.cabi.svc.cluster.local:8000"},
    {"slug": "paris", "brand": "Vélib'", "city": "Paris", "flag": "🗼",
     "domain": "velib.kardol.us", "svc": "http://velib-web.velib.svc.cluster.local:8000"},
    {"slug": "cdmx", "brand": "Ecobici", "city": "Mexico City", "flag": "🌮",
     "domain": "ecobici.kardol.us", "svc": "http://ecobici-web.ecobici.svc.cluster.local:8000",
     # Ecobici is the only one of the six that closes overnight (05:00–00:30 local),
     # so its bike count legitimately bottoms out at night. (tz, open_min, close_min)
     "hours": ("America/Mexico_City", 5 * 60, 0 * 60 + 30)},
    {"slug": "chicago", "brand": "Divvy", "city": "Chicago", "flag": "🌆",
     "domain": "divvy.kardol.us", "svc": "http://divvy-web.divvy.svc.cluster.local:8000"},
    {"slug": "barcelona", "brand": "Bicing", "city": "Barcelona", "flag": "🏖️",
     "domain": "bicing.kardol.us", "svc": "http://bicing-web.bicing.svc.cluster.local:8000"},
    {"slug": "baires", "brand": "Ecobici", "city": "Buenos Aires", "flag": "🧉",
     "domain": "ecobici-ba.kardol.us", "svc": "http://ecobici-ba-web.ecobici-ba.svc.cluster.local:8000"},
    {"slug": "london", "brand": "Santander Cycles", "city": "London", "flag": "🎡",
     "domain": "boris.kardol.us", "svc": "http://boris-web.boris.svc.cluster.local:8000"},
]

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#22c47e"/>'
    '<g transform="translate(2 2) scale(.83)" fill="none" stroke="#0d1117" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="18.5" cy="17.5" r="3.5"/><circle cx="5.5" cy="17.5" r="3.5"/>'
    '<circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/></g></svg>'
)


# Short, distinct row labels for the cross-city boards (the two Ecobicis differ by city).
_SHORT = {"New York + Jersey": "New York", "Washington, DC": "Washington DC"}


def _short(c):
    return _SHORT.get(c["city"], c["city"])


async def _get(client, url):
    try:
        return (await client.get(url, timeout=4.0)).json()
    except Exception:
        return None


async def _fetch(client, c):
    # Each city app exposes the same endpoints (everywhere scope) — pull live state + the two
    # window metrics the cross-city boards need; degrade per-field if any call fails/times out.
    base = c["svc"]
    now, turn, rel = await asyncio.gather(
        _get(client, f"{base}/api/now?neighborhood=everywhere"),
        _get(client, f"{base}/api/turnover?neighborhood=everywhere"),
        _get(client, f"{base}/api/reliability_nbhd?neighborhood=everywhere"),
    )
    out = {**c, "ok": False, "bikes": None, "stations": None,
           "fill": None, "turnover": None, "reliability": None}
    if isinstance(now, dict) and now.get("summary"):
        s = now["summary"]
        b, e, d = (s.get("bikes_available") or 0), (s.get("ebikes_available") or 0), (s.get("docks_available") or 0)
        out["bikes"] = b + e
        out["stations"] = s.get("stations") or 0
        cap = b + e + d
        out["fill"] = round(100 * (b + e) / cap) if cap else None
        out["ok"] = True
    if isinstance(turn, list):
        vals = [x["per_bike_day"] for x in turn if x.get("per_bike_day")]
        if vals:
            out["turnover"] = round(sum(vals) / len(vals), 1)  # mean across areas
    if isinstance(rel, list):
        vals = [x["pct"] for x in rel if x.get("pct") is not None]
        if vals:
            out["reliability"] = round(sum(vals) / len(vals))
    return out


def _board(title, sub, rows, key, suffix):
    """A ranked horizontal-bar leaderboard of cities by `key` (highest first)."""
    rows = [c for c in rows if c.get(key) is not None]
    if not rows:
        return ""
    rows.sort(key=lambda c: -c[key])
    mx = rows[0][key] or 1
    bars = "".join(
        f'<div class="prow"><span class="pcity">{c["flag"]} {_short(c)}</span>'
        f'<span class="ptrack"><span class="pbar" style="width:{max(4, round(100 * c[key] / mx))}%"></span></span>'
        f'<span class="pval">{c[key]}{suffix}</span></div>'
        for c in rows
    )
    return f'<div class="pcard"><h3>{title}</h3><p class="psub">{sub}</p>{bars}</div>'


async def _gather():
    async with httpx.AsyncClient() as client:
        return await asyncio.gather(*(_fetch(client, c) for c in CITIES))


def _service(c):
    """For a city with overnight hours, return (closed_now, 'opens 5 AM'); else (False, '').
    Handles a service window that wraps past midnight (e.g. Ecobici 05:00–00:30)."""
    h = c.get("hours")
    if not h:
        return False, ""
    tz, open_min, close_min = h
    now = datetime.now(ZoneInfo(tz))
    t = now.hour * 60 + now.minute
    is_open = (open_min <= t < close_min) if open_min < close_min else (t >= open_min or t < close_min)
    oh, om = divmod(open_min, 60)
    label = f"{oh % 12 or 12} {'AM' if oh < 12 else 'PM'}" if om == 0 else f"{oh:02d}:{om:02d}"
    return (not is_open), f"opens {label}"


def _card(c):
    closed, opens = _service(c) if c["ok"] else (False, "")
    n = f'{c["bikes"]:,}' if c["ok"] else "—"
    st = f'{c["stations"]:,} stations' if c["ok"] else "offline"
    if not c["ok"]:
        sub, ind, big = "bikes available now", '<span class="off">offline</span>', "big"
    elif closed:
        sub = f'closed overnight · {opens}'
        ind = '<span class="zzz">●</span>closed'
        big = "big dim"
    else:
        sub, ind, big = "bikes available now", '<span class="live"></span>live', "big"
    return f"""
    <a class="card" href="https://{c['domain']}">
      <div class="chead"><span class="flag">{c['flag']}</span>
        <div><div class="brand">{c['brand']}</div><div class="city">{c['city']}</div></div></div>
      <div class="{big}">{n}</div>
      <div class="sub">{sub}</div>
      <div class="foot"><span>{st}</span><span class="ind">{ind}</span></div>
    </a>"""


async def home(request):
    cities = await _gather()
    total = sum(c["bikes"] for c in cities if c["ok"])
    up = sum(1 for c in cities if c["ok"])
    cards = "".join(_card(c) for c in cities)
    # Cross-city patterns. Busiest/reliability are window metrics (all cities); the live "fullest"
    # board excludes systems that are currently closed overnight (their live fill would read ~0).
    open_now = [c for c in cities if not _service(c)[0]]
    patterns = (
        '<p class="psec-h">Cross-city patterns</p><div class="pgrid">'
        + _board("Busiest", "bike-movements per available bike · per day", cities, "turnover", "/day")
        + _board("Fullest right now", "bikes ÷ capacity, open systems", open_now, "fill", "%")
        + _board("Most reliable", "% of time you can grab a bike AND return one", cities, "reliability", "%")
        + "</div>"
    )
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bikeshare trackers · live dock availability</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="description" content="Live bikeshare dock availability and 90-day patterns across New York, Washington DC, Paris, Mexico City, Chicago, Barcelona, Buenos Aires and London.">
<meta property="og:title" content="Bikeshare trackers">
<meta property="og:description" content="Live dock availability across New York, Washington DC, Paris, Mexico City, Chicago, Barcelona, Buenos Aires & London.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://bikes.kardol.us">
<meta property="og:image" content="https://bikes.kardol.us/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Bikeshare trackers">
<meta name="twitter:image" content="https://bikes.kardol.us/og.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<meta http-equiv="refresh" content="60">
<style>
:root{{--bg:#0d1117;--fg:#e2e8f0;--meta:#8b949e;--card:#161b22;--border:#21262d;--border2:#30363d;--accent:#22c47e}}
*{{box-sizing:border-box}}
body{{font-family:'DM Sans',system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;padding:56px 20px}}
.wrap{{width:100%;max-width:1000px}}
.hero{{display:flex;align-items:center;gap:12px;margin-bottom:6px}}
.logo{{width:34px;height:34px}}
h1{{font-family:'Space Grotesk',sans-serif;font-size:28px;font-weight:700;margin:0}}
.tag{{color:var(--meta);font-size:15px;margin:0 0 4px}}
.totals{{color:var(--meta);font-size:13px;font-family:'JetBrains Mono',monospace;margin-bottom:28px}}
.totals b{{color:var(--accent)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
.card{{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--border);
  border-radius:14px;padding:20px;transition:transform .15s,border-color .15s,box-shadow .15s}}
.card:hover{{transform:translateY(-3px);border-color:var(--accent);box-shadow:0 8px 24px rgba(0,0,0,.25)}}
.chead{{display:flex;align-items:center;gap:11px;margin-bottom:14px}}
.flag{{font-size:26px}}
.brand{{font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:17px}}
.city{{color:var(--meta);font-size:12.5px}}
.big{{font-family:'Space Grotesk',sans-serif;font-size:42px;font-weight:700;line-height:1;color:var(--accent)}}
.sub{{color:var(--meta);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:6px}}
.foot{{display:flex;justify-content:space-between;align-items:center;margin-top:16px;font-size:12px;
  color:var(--meta);font-family:'JetBrains Mono',monospace}}
.ind{{display:flex;align-items:center;gap:5px}}
.live{{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 0 rgba(34,196,126,.6);animation:p 2s infinite}}
.off{{color:#f0796a}}
.big.dim{{color:var(--meta)}}
.zzz{{color:#d8a200;font-size:9px;line-height:1}}
@keyframes p{{0%{{box-shadow:0 0 0 0 rgba(34,196,126,.5)}}70%{{box-shadow:0 0 0 6px rgba(34,196,126,0)}}100%{{box-shadow:0 0 0 0 rgba(34,196,126,0)}}}}
.patterns{{margin-top:40px}}
.psec-h{{font-family:'Space Grotesk',sans-serif;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--meta);margin:0 0 14px}}
.pgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px}}
.pcard{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 20px}}
.pcard h3{{font-family:'Space Grotesk',sans-serif;font-size:15px;font-weight:600;margin:0 0 2px}}
.psub{{color:var(--meta);font-size:11.5px;margin:0 0 14px}}
.prow{{display:grid;grid-template-columns:118px 1fr auto;align-items:center;gap:10px;margin:8px 0;font-size:12.5px}}
.pcity{{display:flex;align-items:center;gap:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.ptrack{{height:7px;background:#21262d;border-radius:4px;overflow:hidden}}
.pbar{{height:100%;background:var(--accent);border-radius:4px}}
.pval{{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--fg)}}
footer{{color:var(--meta);font-size:12px;margin-top:36px;font-family:'JetBrains Mono',monospace;text-align:center}}
footer a{{color:var(--accent);text-decoration:none}}
</style></head><body><div class="wrap">
  <div class="hero"><svg class="logo" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18.5" cy="17.5" r="3.5"/><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/></svg><h1>Bikeshare trackers</h1></div>
  <p class="tag">Live dock availability &amp; 90-day patterns, city by city.</p>
  <p class="totals"><b>{total:,}</b> bikes available now across <b>{up}</b> live systems</p>
  <div class="grid">{cards}</div>
  <div class="patterns">{patterns}</div>
  <footer>unofficial · built on public GBFS feeds · <a href="https://kardol.us">kardol.us</a> homelab</footer>
</div></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def favicon(r):
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


async def og(r):
    return Response(_OG, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def healthz(r):
    return PlainTextResponse("ok")


app = Starlette(routes=[
    Route("/", home),
    Route("/favicon.svg", favicon),
    Route("/og.png", og),
    Route("/healthz", healthz),
    Route("/ready", healthz),
])


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
