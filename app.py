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

# ── i18n (same en/es/fr + browser-detection logic as the city apps) ──
SUPPORTED = ("en", "es", "fr")
_LANG_NAMES = {"en": "English", "es": "Español", "fr": "Français"}
MESSAGES = {
    "en": {"title": "Bikeshare trackers · live dock availability", "meta_desc": "Live bikeshare dock availability and 90-day patterns across New York, Washington DC, Paris, Mexico City, Chicago, Barcelona, Buenos Aires and London.", "h1": "Bikeshare trackers", "tag": "Live dock availability & 90-day patterns, city by city.", "totals": "{total} bikes available now across {up} live systems", "card_sub": "bikes available now", "card_closed_sub": "closed overnight · {opens}", "svc_opens": "opens {time}", "ind_live": "live", "ind_closed": "closed", "ind_offline": "offline", "stations": "stations", "patterns_h": "Cross-city patterns", "b_busiest_t": "Busiest", "b_busiest_s": "bike-movements per available bike · per day", "b_full_t": "Fullest right now", "b_full_s": "bikes ÷ capacity, open systems", "b_rel_t": "Most reliable", "b_rel_s": "% of time you can grab a bike AND return one", "per_day": "/day", "footer": "unofficial · built on public GBFS feeds · {link} homelab", "lang_aria": "Language"},
    "es": {"title": "Monitores de bicis compartidas · disponibilidad de anclajes en vivo", "meta_desc": "Disponibilidad de anclajes en vivo y patrones de 90 días en New York, Washington DC, Paris, Mexico City, Chicago, Barcelona, Buenos Aires y London.", "h1": "Monitores de bicis compartidas", "tag": "Disponibilidad de anclajes en vivo y patrones de 90 días, ciudad por ciudad.", "totals": "{total} bicis disponibles ahora en {up} sistemas en vivo", "card_sub": "bicis disponibles ahora", "card_closed_sub": "cerrado por la noche · {opens}", "svc_opens": "abre {time}", "ind_live": "en vivo", "ind_closed": "cerrado", "ind_offline": "sin conexión", "stations": "estaciones", "patterns_h": "Patrones entre ciudades", "b_busiest_t": "Más activo", "b_busiest_s": "movimientos de bici por bici disponible · por día", "b_full_t": "Más lleno ahora", "b_full_s": "bicis ÷ capacidad, sistemas abiertos", "b_rel_t": "Más confiable", "b_rel_s": "% del tiempo en que puedes tomar una bici Y devolver una", "per_day": "/día", "footer": "no oficial · basado en feeds públicos GBFS · {link} homelab", "lang_aria": "Idioma"},
    "fr": {"title": "Suivi vélos en libre-service · bornes libres en direct", "meta_desc": "Disponibilité des bornes libres en direct et tendances sur 90 jours à New York, Washington DC, Paris, Mexico City, Chicago, Barcelona, Buenos Aires et London.", "h1": "Suivi vélos en libre-service", "tag": "Bornes libres en direct et tendances sur 90 jours, ville par ville.", "totals": "{total} vélos disponibles maintenant sur {up} systèmes en direct", "card_sub": "vélos disponibles maintenant", "card_closed_sub": "fermé la nuit · {opens}", "svc_opens": "ouvre à {time}", "ind_live": "en direct", "ind_closed": "fermé", "ind_offline": "hors ligne", "stations": "stations", "patterns_h": "Tendances entre villes", "b_busiest_t": "Plus actif", "b_busiest_s": "mouvements de vélos par vélo disponible · par jour", "b_full_t": "Le plus plein maintenant", "b_full_s": "vélos ÷ capacité, systèmes ouverts", "b_rel_t": "Le plus fiable", "b_rel_s": "% du temps où vous pouvez prendre ET rendre un vélo", "per_day": "/jour", "footer": "non officiel · basé sur des flux publics GBFS · {link} homelab", "lang_aria": "Langue"},
}


def _norm(lang):
    if not lang:
        return None
    code = lang.strip().lower().split("-")[0]
    return code if code in SUPPORTED else None


def _accept_lang(header):
    if not header:
        return None
    parsed = []
    for i, part in enumerate(header.split(",")):
        tok = part.split(";")
        q = 1.0
        for p in tok[1:]:
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        parsed.append((q, i, tok[0].strip()))
    for _q, _i, tag in sorted(parsed, key=lambda x: (-x[0], x[1])):
        c = _norm(tag)
        if c:
            return c
    return None


def pick_lang(request):
    # ?lang= → cookie → Accept-Language (browser wins when supported) → English.
    return (_norm(request.query_params.get("lang")) or _norm(request.cookies.get("lang"))
            or _accept_lang(request.headers.get("accept-language")) or "en")


def t(lang, key, **fmt):
    v = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"].get(key, key)
    return v.format(**fmt) if fmt else v


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


# Theme: default to the OS/browser preference (prefers-color-scheme); a manual toggle overrides
# and is remembered in localStorage. Runs before paint (no flash); keeps following the OS live
# until the visitor makes an explicit choice. Module constant so the JS braces aren't f-string-escaped.
_THEME_JS = """<script>
(function(){
  try {
    var s = localStorage.getItem('theme');
    var os = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (s ? s === 'dark' : os) document.documentElement.classList.add('dark');
  } catch (e) {}
})();
function toggleTheme(){
  var d = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('theme', d ? 'dark' : 'light'); } catch (e) {}
}
try {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e){
    if (!localStorage.getItem('theme')) document.documentElement.classList.toggle('dark', e.matches);
  });
} catch (e) {}
</script>"""

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
    """For a city with overnight hours, return (closed_now, open_time_24h); else (False, '').
    open_time is locale-neutral (e.g. '5:00'); the caller localizes via svc_opens. Handles a
    window that wraps past midnight (e.g. Ecobici 05:00–00:30)."""
    h = c.get("hours")
    if not h:
        return False, ""
    tz, open_min, close_min = h
    now = datetime.now(ZoneInfo(tz))
    mins = now.hour * 60 + now.minute
    is_open = (open_min <= mins < close_min) if open_min < close_min else (mins >= open_min or mins < close_min)
    oh, om = divmod(open_min, 60)
    return (not is_open), f"{oh}:{om:02d}"


def _card(c, lang):
    closed, open_t = _service(c) if c["ok"] else (False, "")
    n = f'{c["bikes"]:,}' if c["ok"] else "—"
    st = (f'{c["stations"]:,} ' + t(lang, "stations")) if c["ok"] else t(lang, "ind_offline")
    if not c["ok"]:
        sub, ind, big = t(lang, "card_sub"), f'<span class="off">{t(lang, "ind_offline")}</span>', "big"
    elif closed:
        sub = t(lang, "card_closed_sub", opens=t(lang, "svc_opens", time=open_t))
        ind = f'<span class="zzz">●</span>{t(lang, "ind_closed")}'
        big = "big dim"
    else:
        sub, ind, big = t(lang, "card_sub"), f'<span class="live"></span>{t(lang, "ind_live")}', "big"
    return f"""
    <a class="card" href="https://{c['domain']}">
      <div class="chead"><span class="flag">{c['flag']}</span>
        <div><div class="brand">{c['brand']}</div><div class="city">{c['city']}</div></div></div>
      <div class="{big}">{n}</div>
      <div class="sub">{sub}</div>
      <div class="foot"><span>{st}</span><span class="ind">{ind}</span></div>
    </a>"""


async def home(request):
    lang = pick_lang(request)
    cities = await _gather()
    total = sum(c["bikes"] for c in cities if c["ok"])
    up = sum(1 for c in cities if c["ok"])
    cards = "".join(_card(c, lang) for c in cities)
    # Cross-city patterns. Busiest/reliability are window metrics (all cities); the live "fullest"
    # board excludes systems that are currently closed overnight (their live fill would read ~0).
    open_now = [c for c in cities if not _service(c)[0]]
    patterns = (
        f'<p class="psec-h">{t(lang, "patterns_h")}</p><div class="pgrid">'
        + _board(t(lang, "b_busiest_t"), t(lang, "b_busiest_s"), cities, "turnover", t(lang, "per_day"))
        + _board(t(lang, "b_full_t"), t(lang, "b_full_s"), open_now, "fill", "%")
        + _board(t(lang, "b_rel_t"), t(lang, "b_rel_s"), cities, "reliability", "%")
        + "</div>"
    )
    picker = (
        f'<div class="topbar"><select class="lang-pick" aria-label="{t(lang, "lang_aria")}" '
        f'onchange="location.search=\'?lang=\'+this.value">'
        + "".join(f'<option value="{code}"{" selected" if code == lang else ""}>{name}</option>'
                  for code, name in _LANG_NAMES.items())
        + '</select>'
        + '<button class="theme-toggle" onclick="toggleTheme()" title="Toggle light/dark" aria-label="Toggle light/dark">◐</button>'
        + "</div>"
    )
    html = f"""<!doctype html><html lang="{lang}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{_THEME_JS}
<title>{t(lang, "title")}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="description" content="{t(lang, "meta_desc")}">
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
:root{{--bg:#f8f9fa;--fg:#14171e;--meta:#6f737b;--card:#f3f4f6;--border:#e2e5e8;--border2:#c8cdd3;--accent:#1da46c;--track:#e2e6ea;--bad:#c0392b}}
html.dark{{--bg:#0d1117;--fg:#e2e8f0;--meta:#8b949e;--card:#161b22;--border:#21262d;--border2:#30363d;--accent:#22c47e;--track:#21262d;--bad:#f0796a}}
*{{box-sizing:border-box}}
body{{font-family:'DM Sans',system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;padding:56px 20px;transition:background .2s,color .2s}}
.wrap{{width:100%;max-width:1000px}}
.topbar{{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-bottom:10px}}
.lang-pick{{height:32px;box-sizing:border-box;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;font-family:inherit;font-size:13px;color:var(--fg);padding:0 26px 0 10px;appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%238b949e' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 9px center}}
.lang-pick:hover{{border-color:var(--accent)}}
.theme-toggle{{height:32px;box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;font-size:15px;line-height:1;color:var(--fg);padding:0 10px}}
.theme-toggle:hover{{border-color:var(--accent)}}
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
.off{{color:var(--bad)}}
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
.ptrack{{height:7px;background:var(--track);border-radius:4px;overflow:hidden}}
.pbar{{height:100%;background:var(--accent);border-radius:4px}}
.pval{{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--fg)}}
footer{{color:var(--meta);font-size:12px;margin-top:36px;font-family:'JetBrains Mono',monospace;text-align:center}}
footer a{{color:var(--accent);text-decoration:none}}
</style></head><body><div class="wrap">
  {picker}
  <div class="hero"><svg class="logo" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18.5" cy="17.5" r="3.5"/><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/></svg><h1>{t(lang, "h1")}</h1></div>
  <p class="tag">{t(lang, "tag")}</p>
  <p class="totals">{t(lang, "totals", total=f"<b>{total:,}</b>", up=f"<b>{up}</b>")}</p>
  <div class="grid">{cards}</div>
  <div class="patterns">{patterns}</div>
  <footer>{t(lang, "footer", link='<a href="https://kardol.us">kardol.us</a>')}</footer>
</div></body></html>"""
    resp = HTMLResponse(html, headers={"Cache-Control": "no-store", "Vary": "Cookie, Accept-Language"})
    if _norm(request.query_params.get("lang")):
        resp.set_cookie("lang", lang, max_age=31536000, path="/", samesite="lax")
    return resp


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
