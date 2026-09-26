#!/usr/bin/env python3
"""Scan a movies folder, look up metadata (OMDb), write movies.html.

Usage: python3 build.py [/Volumes/movies]
Key: OMDB_API_KEY in env or in ./.env
Results are cached in cache.json, so re-runs only look up new movies.
"""
from datetime import datetime
import html, json, os, re, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/movies")
SKIP_DIRS = {"TV", "tmp"}
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts"}
CACHE = HERE / "cache.json"


def load_env():
    env = dict(os.environ)
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("\"'"))
    return env


ENV = load_env()
OMDB = ENV.get("OMDB_API_KEY")

JUNK = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|web-?dl|web-?rip|webrip|hdrip|hdtv|"
    r"dvdrip|x26[45]|h\.?26[45]|hevc|10bit|8bit|aac\S*|ddp\S*|dts\S*|atmos|5\.1|7\.1|extended|"
    r"remastered|imax|repack|proper|dual|amzn|nf|itunes|yts|yify|rarbg)\b.*",
    re.I,
)


def parse_name(name):
    """Return (title, year|None) from a messy release name."""
    base = re.sub(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts)$", "", name, flags=re.I)
    base = re.sub(r"\[[^\]]*\]", " ", base)
    base = re.sub(r"[._]", " ", base)
    m = None
    for m in re.finditer(r"[\(\s]((?:19|20)\d\d)\b", " " + base):
        pass  # take the last plausible year
    if m and base.rstrip().endswith(m.group(1)) and "(" not in m.group(0):
        m = None  # bare trailing number is likely part of the title (e.g. "Blade Runner 2049")
    year = int(m.group(1)) if m else None
    title = base[: max(m.start() - 1, 0)] if m else JUNK.sub("", base)
    title = JUNK.sub("", title)
    title = re.sub(r"[\(\)\-]+\s*$", "", title).strip(" -(")
    return title.strip(), year


def scan():
    found = {}
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
            continue
        for e in sorted(d.iterdir()):
            if e.name.startswith(".") or e.name in {"@eaDir", "#recycle"}:
                continue
            if e.is_dir() or e.suffix.lower() in VIDEO:
                title, year = parse_name(e.name)
                if title:
                    found[f"{d.name}/{e.name}"] = {"title": title, "year": year, "path": f"{d.name}/{e.name}"}
    return list(found.values())


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def iso_date(v):
    try:
        return datetime.strptime(v, "%d %b %Y").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def backfill_date(m):
    """Add the release date to a cached entry, by IMDb id."""
    try:
        m["released"] = iso_date(get(f"https://www.omdbapi.com/?apikey={OMDB}&i={m['imdb_id']}").get("Released"))
    except Exception:
        pass
    return m


def lookup(m):
    out = dict(m, found=False)
    try:
        def q(**kw):
            kw.update(apikey=OMDB, type="movie", plot="short")
            return get("https://www.omdbapi.com/?" + urllib.parse.urlencode(kw))
        o = q(t=m["title"], y=m["year"]) if m["year"] else q(t=m["title"])
        if o.get("Response") == "False" and m["year"]:  # year in filename may be off
            o = q(t=m["title"])
        if o.get("Response") == "False":
            hits = q(s=m["title"]).get("Search")
            o = q(i=hits[0]["imdbID"]) if hits else o
        if o.get("Response") == "False":
            return out
        na = lambda v: None if v in (None, "N/A") else v
        rt = next((r["Value"] for r in o.get("Ratings", []) if r["Source"] == "Rotten Tomatoes"), None)
        out.update(
            found=True,
            name=o["Title"],
            year=(na(o.get("Year")) or "")[:4],
            overview=na(o.get("Plot")) or "",
            poster=na(o.get("Poster")),
            genres=(na(o.get("Genre")) or "").split(", ") if na(o.get("Genre")) else [],
            runtime=(na(o.get("Runtime")) or "").replace(" min", "") or None,
            imdb_id=o.get("imdbID"),
            released=iso_date(o.get("Released")),
            imdb=na(o.get("imdbRating")),
            rt=rt,
        )
    except Exception as ex:
        out["error"] = str(ex)
    return out


def link(m):
    return "file://" + urllib.parse.quote(str(ROOT / m["path"]))


def card(m):
    if not m.get("found"):
        return (f'<div class="card miss"><a class="poster" href="{link(m)}" target="_blank"></a><div class="body"><h2>{html.escape(m["title"])}</h2>'
                f'<p class="meta">No match found</p><p class="meta">{html.escape(m["path"])}</p></div></div>')
    poster = f'<img loading="lazy" src="{m["poster"]}" alt="">' if m.get("poster") else ""
    badges = ""
    if m.get("imdb"):
        badges += f'<a class="b imdb" href="https://www.imdb.com/title/{m["imdb_id"]}/" target="_blank">IMDb {m["imdb"]}</a>'
    if m.get("rt"):
        badges += f'<span class="b rt">🍅 {m["rt"]}</span>'
    meta = " · ".join(filter(None, [m.get("released") or m["year"], f'{m["runtime"]} min' if m.get("runtime") else "", ", ".join(m["genres"][:3])]))
    return (f'<div class="card" data-t="{html.escape(m["name"].lower())}" data-imdb="{m.get("imdb") or 0}" '
            f'data-rt="{(m.get("rt") or "0").rstrip("%")}" data-y="{m["year"]}" data-d="{m.get("released") or m["year"] + "-00-00"}">'
            f'<a class="poster" href="{link(m)}" target="_blank" title="Open folder">{poster}</a><div class="body"><h2>{html.escape(m["name"])}</h2>'
            f'<p class="meta">{html.escape(meta)}</p><div class="badges">{badges}</div>'
            f'<p class="intro">{html.escape(m["overview"])}</p>'
            f'<p class="path">{html.escape(m["path"])}</p></div></div>')


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Movies</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%237c3aed'/%3E%3Cstop offset='1' stop-color='%23f97316'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url%28%23g%29'/%3E%3Crect x='6' y='27' width='52' height='30' rx='4' fill='%23fff'/%3E%3Crect x='6' y='11' width='52' height='13' rx='3' fill='%231f2937'/%3E%3Cpolygon points='12%2C11 22%2C11 16%2C24 6%2C24' fill='%23facc15'/%3E%3Cpolygon points='26%2C11 36%2C11 30%2C24 20%2C24' fill='%2322d3ee'/%3E%3Cpolygon points='40%2C11 50%2C11 44%2C24 34%2C24' fill='%23f43f5e'/%3E%3Cpolygon points='54%2C11 58%2C11 58%2C24 48%2C24' fill='%234ade80'/%3E%3Cpolygon points='26%2C33 26%2C52 44%2C42.5' fill='%23f43f5e'/%3E%3C/svg%3E"><style>
:root{--bg:#fff;--fg:#1a1a1a;--card:#f4f4f6;--mut:#6b6b76}
@media(prefers-color-scheme:dark){:root{--bg:#141417;--fg:#eee;--card:#1f1f24;--mut:#9a9aa6}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;z-index:1;border-bottom:1px solid var(--card)}
header h1{font-size:18px;margin:0 8px 0 0}input,select{padding:6px 10px;font:inherit;border-radius:6px;border:1px solid var(--mut);background:var(--card);color:var(--fg)}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:16px;padding:16px}
.card{display:flex;gap:14px;background:var(--card);border-radius:10px;overflow:hidden}
.poster{display:block;flex:0 0 240px;min-height:360px;background:#0002}.poster img{width:240px;height:100%;object-fit:cover;display:block}
.body{padding:12px 12px 12px 0;min-width:0}h2{margin:0;font-size:17px}.meta,.path{margin:2px 0;color:var(--mut);font-size:13px}
.path{font-size:11px;word-break:break-all}.intro{margin:8px 0;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.badges{display:flex;gap:6px;margin-top:6px}.b{font-size:12px;font-weight:600;padding:2px 8px;border-radius:5px;text-decoration:none}
.imdb{background:#f5c518;color:#000}.rt{background:#fa320a;color:#fff}.miss{opacity:.6}
@media(max-width:600px){main{grid-template-columns:1fr}.poster,.poster img{flex-basis:150px;width:150px}}
</style>
<header><h1>Movies (__N__)</h1><input id="q" placeholder="Search…"><select id="s">
<option value="imdb" selected>IMDb score</option><option value="t">Name (A–Z)</option><option value="d">Release date (newest)</option><option value="rt">Rotten Tomatoes</option></select><select id="yr"></select></header>
<main id="m">__CARDS__</main>
<script>
const m=document.getElementById('m'),cards=[...m.children];
const yr=document.getElementById('yr'),years=[...new Set(cards.map(c=>c.dataset.y).filter(Boolean))].sort().reverse();
yr.innerHTML='<option value="">All years</option>'+years.map(y=>`<option>${y}</option>`).join('');
const cur=String(new Date().getFullYear());yr.value=years.includes(cur)?cur:'';
function go(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value,y=yr.value;
cards.forEach(c=>c.style.display=(c.dataset.t||c.textContent.toLowerCase()).includes(q)&&(!y||c.dataset.y===y)?'':'none');
[...cards].sort((a,b)=>s=='t'?(a.dataset.t||'~').localeCompare(b.dataset.t||'~'):s=='d'?(b.dataset.d||'').localeCompare(a.dataset.d||''):(+b.dataset[s]||0)-(+a.dataset[s]||0)).forEach(c=>m.appendChild(c))}
q.oninput=s.onchange=yr.onchange=go;go();
</script>"""


def main():
    if not OMDB:
        sys.exit("Set OMDB_API_KEY in env or .env")
    if not ROOT.is_dir():
        sys.exit(f"{ROOT} not found - is the NAS mounted?")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    movies = scan()
    todo = [m for m in movies if m["path"] not in cache or not cache[m["path"]].get("found") and "error" in cache[m["path"]]]
    print(f"{len(movies)} entries, {len(todo)} to look up")
    with ThreadPoolExecutor(8) as ex:
        for i, r in enumerate(ex.map(lookup, todo), 1):
            cache[r["path"]] = r
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}")
    old = [c for c in cache.values() if c.get("imdb_id") and "released" not in c]
    if old:
        print(f"adding release dates to {len(old)} cached movies")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(backfill_date, old))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    rows = [cache[m["path"]] for m in movies]
    rows.sort(key=lambda r: (r.get("name") or r["title"]).lower())
    (HERE / "movies.html").write_text(PAGE.replace("__N__", str(len(rows))).replace("__CARDS__", "".join(map(card, rows))))
    print(f"unmatched: {sum(not r.get('found') for r in rows)}  -> {HERE/'movies.html'}")


if __name__ == "__main__":
    main()
