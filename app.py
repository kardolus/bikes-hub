"""bikes.kardol.us — the hub front door for the per-city bikeshare trackers.

Server-side aggregates each city's live "bikes available now" + station count by fetching
its in-cluster /api/now?neighborhood=everywhere (no CORS needed), and renders a flightdeck-
styled directory. Cities that are unreachable degrade to "—". Auto-refreshes every 60s.
"""
import asyncio
from pathlib import Path

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
     "domain": "ecobici.kardol.us", "svc": "http://ecobici-web.ecobici.svc.cluster.local:8000"},
    {"slug": "chicago", "brand": "Divvy", "city": "Chicago", "flag": "🌆",
     "domain": "divvy.kardol.us", "svc": "http://divvy-web.divvy.svc.cluster.local:8000"},
    {"slug": "barcelona", "brand": "Bicing", "city": "Barcelona", "flag": "🏖️",
     "domain": "bicing.kardol.us", "svc": "http://bicing-web.bicing.svc.cluster.local:8000"},
]

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#22c47e"/>'
    '<g transform="translate(2 2) scale(.83)" fill="none" stroke="#0d1117" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="18.5" cy="17.5" r="3.5"/><circle cx="5.5" cy="17.5" r="3.5"/>'
    '<circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/></g></svg>'
)


async def _fetch(client, c):
    try:
        r = await client.get(f"{c['svc']}/api/now?neighborhood=everywhere", timeout=4.0)
        s = r.json()["summary"]
        bikes = (s.get("bikes_available") or 0) + (s.get("ebikes_available") or 0)
        return {**c, "bikes": bikes, "stations": s.get("stations") or 0, "ok": True}
    except Exception:
        return {**c, "bikes": None, "stations": None, "ok": False}


async def _gather():
    async with httpx.AsyncClient() as client:
        return await asyncio.gather(*(_fetch(client, c) for c in CITIES))


def _card(c):
    n = f'{c["bikes"]:,}' if c["ok"] else "—"
    st = f'{c["stations"]:,} stations' if c["ok"] else "offline"
    live = '<span class="live"></span>live' if c["ok"] else '<span class="off">offline</span>'
    return f"""
    <a class="card" href="https://{c['domain']}">
      <div class="chead"><span class="flag">{c['flag']}</span>
        <div><div class="brand">{c['brand']}</div><div class="city">{c['city']}</div></div></div>
      <div class="big">{n}</div>
      <div class="sub">bikes available now</div>
      <div class="foot"><span>{st}</span><span class="ind">{live}</span></div>
    </a>"""


async def home(request):
    cities = await _gather()
    total = sum(c["bikes"] for c in cities if c["ok"])
    up = sum(1 for c in cities if c["ok"])
    cards = "".join(_card(c) for c in cities)
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bikeshare trackers · live dock availability</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="description" content="Live bikeshare dock availability and 90-day patterns across New York, Washington DC, Paris, Mexico City, Chicago and Barcelona.">
<meta property="og:title" content="Bikeshare trackers">
<meta property="og:description" content="Live dock availability across New York, Washington DC, Paris, Mexico City, Chicago & Barcelona.">
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
@keyframes p{{0%{{box-shadow:0 0 0 0 rgba(34,196,126,.5)}}70%{{box-shadow:0 0 0 6px rgba(34,196,126,0)}}100%{{box-shadow:0 0 0 0 rgba(34,196,126,0)}}}}
footer{{color:var(--meta);font-size:12px;margin-top:36px;font-family:'JetBrains Mono',monospace;text-align:center}}
footer a{{color:var(--accent);text-decoration:none}}
</style></head><body><div class="wrap">
  <div class="hero"><svg class="logo" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18.5" cy="17.5" r="3.5"/><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/></svg><h1>Bikeshare trackers</h1></div>
  <p class="tag">Live dock availability &amp; 90-day patterns, city by city.</p>
  <p class="totals"><b>{total:,}</b> bikes available now across <b>{up}</b> live systems</p>
  <div class="grid">{cards}</div>
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
