#!/usr/bin/env python3
"""Scan a movies folder, look up metadata (OMDb), write movies.html.

Usage: python3 build.py [folder ...]   (default: /Volumes/movies and /Volumes/movies-2T)
Key: OMDB_API_KEY in env or in ./.env
Results are cached in cache.json, so re-runs only look up new movies.
"""
from datetime import datetime
import html, json, os, re, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
ROOTS = [Path(p) for p in sys.argv[1:]] or [Path("/Volumes/movies"), Path("/Volumes/movies-2T")]
SKIP_DIRS = {"TV", "tmp", "TVseries", "upload", "4k", "movie-catalog"}
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts"}
CACHE = HERE / "cache.json"
DISCOVER_CACHE = HERE / "discover_cache.json"
OVERRIDES = json.loads((HERE / "overrides.json").read_text()) if (HERE / "overrides.json").exists() else {}


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
TMDB = ENV.get("TMDB_API_KEY")
OPENSUBTITLES = ENV.get("OPENSUBTITLES_API_KEY")
SUBTITLE_EXT = {".srt", ".vtt", ".ass", ".ssa"}

JUNK = re.compile(
    r"\b(3d|hsbs|half-?sbs|sbs|3dtv|hc|2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|web-?dl|web-?rip|webrip|hdrip|hdtv|"
    r"dvdrip|x26[45]|h\.?26[45]|hevc|10bit|8bit|aac\S*|ddp\S*|dts\S*|atmos|5\.1|7\.1|extended|"
    r"remastered|imax|repack|proper|dual|amzn|nf|itunes|yts|yify|rarbg)\b.*",
    re.I,
)


CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_CUT = re.compile(r"(?i)(HDTC|HD|BD|TC|DVD|TS|WEB|BluRay|\d{3,4}p|4K)")


def parse_name(name):
    """Return (title, year|None) from a messy release name."""
    if CJK.search(name):  # e.g. "2017水形物语HD1080P中字.mp4": year, Chinese title, then quality tags
        base = re.sub(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts)$", "", name, flags=re.I)
        ym = re.match(r"((?:19|20)\d\d)[\s._-]*", base)
        year = int(ym.group(1)) if ym else None
        rest = base[ym.end():] if ym else base
        cut = CJK_CUT.search(rest)
        rest = (rest[: cut.start()] if cut else rest).strip(" -_.")
        rest = re.split(r"\s+", rest)[0]  # later words are site ads / dub notes
        parts = []
        for seg in rest.split("."):  # "神奇动物.格林德沃之罪.2018": keep Chinese segments, stop at Latin/year
            if not CJK.search(seg):
                ys = re.fullmatch(r"(?:19|20)\d\d", seg)
                year = year or (int(seg) if ys else None)
                if parts:
                    break
                continue
            parts.append(seg)
        return " ".join(parts) or rest, year
    base = re.sub(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts)$", "", name, flags=re.I)
    base = re.sub(r"\[[^\]]*\]", " ", base)
    base = re.sub(r"[._]", " ", base)
    base = re.sub(r"^3D ", "", base)
    lead = re.match(r"((?:19|20)\d\d) (?=\S)", base)
    if lead and re.search(r"(?:19|20)\d\d\D*$", base[5:]):  # "2001 Title 2001": year is a prefix, not the title
        base = base[5:]
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
    for root in ROOTS:
        if not root.is_dir():
            print(f"skipping {root} - not mounted")
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
                continue
            entries = []
            for e in sorted(d.iterdir()):
                if e.is_dir() and re.fullmatch(r"(?:19|20)\d\d( and before)?", e.name):  # cartoon/<year>/<movie>
                    entries += sorted(e.iterdir())
                else:
                    entries.append(e)
            for e in entries:
                if e.name.startswith(".") or e.name in {"@eaDir", "#recycle"}:
                    continue
                if re.search(r"\bS\d{2}E\d{2}\b", e.name, re.I):
                    continue  # TV episode
                if e.is_dir() or e.suffix.lower() in VIDEO:
                    title, year = parse_name(e.name)
                    if title:
                        p = str(e.relative_to(root.parent))  # relative to /Volumes
                        ov = OVERRIDES.get(p)
                        if ov and ov.get("hide"):
                            continue
                        if ov:
                            title, year = ov.get("title", title), ov.get("year", year)
                        # tmdb_lookup/omdb_lookup store a found movie's year as a string (from a
                        # release date), so an unmatched one needs to match that type too - otherwise
                        # a mix of str and int years in the final rows breaks sorting them later.
                        found[p] = {"title": title, "year": str(year) if year is not None else None, "path": p, "ov": ov}
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


def tmdb(path, **params):
    """GET from TMDB; accepts either a v3 API key or a v4 read-access token."""
    req = urllib.request.Request("https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params))
    if len(TMDB) > 40:
        req.add_header("Authorization", "Bearer " + TMDB)
    else:
        req = urllib.request.Request(req.full_url + "&api_key=" + TMDB)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def tmdb_score(d):
    """TMDB user rating as a string like "7.8", or "" when nobody has voted."""
    return f"{d['vote_average']:.1f}" if d.get("vote_count") else ""


def countries_of(d):
    return ", ".join(c["name"] for c in d.get("production_countries", []))


def backfill_au(m):
    """Add the Australian classification, country and TMDB id/score via TMDB, by IMDb id (or a cached TMDB id).
    Reuses m["_tmdb"], the raw TMDB response saved on every lookup, so adding another field later from the
    same data (e.g. a new badge) needs no new API calls - just a re-run to re-derive it from the cache."""
    try:
        tid = m.get("tmdb_id")
        d = m.get("_tmdb")
        if not tid:
            found = tmdb(f"/find/{m['imdb_id']}", external_source="imdb_id")["movie_results"]
            if not found:
                m.setdefault("au", ""), m.setdefault("tmdb", ""), m.setdefault("country", "")
                return m
            tid, m["tmdb"] = found[0]["id"], tmdb_score(found[0])
            m["tmdb_id"] = tid
        if not d:
            d = tmdb(f"/movie/{tid}", append_to_response="release_dates")
            m["_tmdb"] = d
        au = ""
        for c in d.get("release_dates", {}).get("results", []):
            if c["iso_3166_1"] == "AU":
                au = next((x["certification"] for x in c["release_dates"] if x["certification"]), "")
        m["au"] = au
        m["country"] = countries_of(d)
        m.setdefault("tmdb", tmdb_score(d))
    except Exception:
        pass  # left unset, retried next run
    return m


def tmdb_lookup(m):
    """Look up a movie on TMDB alone (no IMDb/RT scores; those are backfilled from OMDb later)."""
    out = dict(m, found=False, tried=True)
    lang = "zh-CN" if CJK.search(m["title"]) else "en-US"
    ov = m.get("ov") or {}
    if ov.get("tmdb"):
        return tmdb_details(out, ov["tmdb"], lang)
    if ov.get("imdb"):
        res = tmdb(f"/find/{ov['imdb']}", external_source="imdb_id")["movie_results"]
        return tmdb_details(out, res[0]["id"], lang) if res else out
    q = {"query": m["title"], "language": lang}
    if m["year"]:
        q["year"] = m["year"]
    res = tmdb("/search/movie", **q)["results"]
    if not res and m["year"]:
        q.pop("year")
        res = tmdb("/search/movie", **q)["results"]
    return tmdb_details(out, res[0]["id"], lang) if res else out


def tmdb_details(out, tid, lang):
    d = tmdb(f"/movie/{tid}", append_to_response="release_dates", language=lang)
    if not d.get("overview"):  # no Chinese synopsis: fall back to English
        d["overview"] = tmdb(f"/movie/{tid}", language="en-US").get("overview", "")
    au = ""
    for c in d.get("release_dates", {}).get("results", []):
        if c["iso_3166_1"] == "AU":
            au = next((x["certification"] for x in c["release_dates"] if x["certification"]), "")
    out.update(
        found=True, src="tmdb", scored=False,
        name=d["title"],
        year=(d.get("release_date") or "")[:4],
        released=d.get("release_date") or None,
        overview=d.get("overview", ""),
        poster=d.get("poster_path") and "https://image.tmdb.org/t/p/w342" + d["poster_path"],
        genres=[g["name"] for g in d.get("genres", [])],
        runtime=str(d["runtime"]) if d.get("runtime") else None,
        imdb_id=d.get("imdb_id") or None,
        imdb=None, rt=None, au=au, tmdb=tmdb_score(d), tmdb_id=tid, country=countries_of(d),
        _tmdb=d,  # raw TMDB response, kept so a future new field can be read from cache with no new API call
    )
    return out


OMDB_DOWN = False


def backfill_scores(m):
    """Add IMDb/RT scores to a TMDB-sourced entry, by IMDb id. Stops trying once OMDb's daily limit is hit."""
    global OMDB_DOWN
    if OMDB_DOWN:
        return m
    try:
        o = get(f"https://www.omdbapi.com/?apikey={OMDB}&i={m['imdb_id']}")
        m["imdb"] = None if o.get("imdbRating", "N/A") == "N/A" else o["imdbRating"]
        m["rt"] = next((r["Value"] for r in o.get("Ratings", []) if r["Source"] == "Rotten Tomatoes"), None)
        m["scored"] = True
    except urllib.error.HTTPError as ex:
        OMDB_DOWN = OMDB_DOWN or ex.code == 401
    except Exception:
        pass
    return m


def manual_entry(m, ov):
    """A fully hand-entered row, for a title that has no matching entry in TMDB's *movie* database at
    all - e.g. a TV special/episode released under its own IMDb id, which TMDB's /find only resolves
    against tv_results, not movie_results, so the usual {"imdb": "tt..."} override never matches
    anything. {"manual": true, "overview": "...", "poster": "https://...", "genres": [...],
    "runtime": "60", "released": "2022-01-01", "imdb": "tt...", ...} in overrides.json skips any
    OMDb/TMDB lookup and uses exactly what's given, defaulting anything left out."""
    return dict(
        m, found=True, tried=True, src="manual", scored=True,
        name=m["title"], overview=ov.get("overview", ""), poster=ov.get("poster"),
        genres=ov.get("genres", []), runtime=ov.get("runtime"), released=ov.get("released"),
        imdb_id=ov.get("imdb"), imdb=ov.get("imdb_score"), rt=ov.get("rt"),
        au=ov.get("au", ""), tmdb=ov.get("tmdb_score"), tmdb_id=ov.get("tmdb_id"),
        country=ov.get("country", ""),
    )


def lookup(m):
    global OMDB_DOWN
    ov = m.get("ov") or {}
    if ov.get("manual"):
        return manual_entry(m, ov)
    if (CJK.search(m["title"]) or ov.get("tmdb") or ov.get("imdb")) and TMDB:  # OMDb can't search Chinese titles
        try:
            out = tmdb_lookup(m)
            if not out["found"]:  # bilingual name, e.g. "功夫熊猫1-3D.Kung.Fu.Panda.1.3D...": retry on the English part
                en = re.sub(r"\[cnliti\]|[^\x00-\x7f]+", " ", m["path"].split("/")[-1])
                title, year = parse_name(en.replace("-3D", " "))
                if title and not CJK.search(title):
                    out = dict(tmdb_lookup(dict(m, title=title, year=year or m["year"])), title=m["title"], year=m["year"])
            return out
        except Exception as ex:
            return dict(m, found=False, error=str(ex))
    try:
        if not OMDB_DOWN:
            out = omdb_lookup(m)
            if out.get("found") or not TMDB:
                return out
    except Exception as ex:
        OMDB_DOWN = OMDB_DOWN or getattr(ex, "code", None) == 401
        if not TMDB:
            return dict(m, found=False, error=str(ex))
    try:  # OMDb failed (daily limit, or no match): try TMDB
        return tmdb_lookup(m)
    except Exception as ex:
        return dict(m, found=False, error=str(ex))


def omdb_lookup(m):
    out = dict(m, found=False)
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
        country=na(o.get("Country")) or "",
    )
    return out


def link(m):
    return "file://" + urllib.parse.quote(str(ROOTS[0].parent / m["path"]))


def card(m):
    if not m.get("found"):
        return (f'<div class="card miss"><a class="poster" href="{link(m)}" target="_blank"></a><div class="body"><h2>{html.escape(m["title"])}</h2>'
                f'<p class="meta">No match found</p><p class="meta">{html.escape(m["path"])}</p></div></div>')
    poster = f'<img loading="lazy" src="{m["poster"]}" alt="">' if m.get("poster") else ""
    poster_tag = (f'<a class="poster" href="{link(m)}" target="_blank" title="Open folder">{poster}</a>' if m.get("path")
                  else f'<div class="poster">{poster}</div>')
    badges = ""
    if m.get("imdb"):
        badges += f'<a class="b imdb" href="https://www.imdb.com/title/{m["imdb_id"]}/" target="_blank">IMDb {m["imdb"]}</a>'
    if m.get("rt"):
        badges += f'<span class="b rt">🍅 {m["rt"]}</span>'
    if m.get("tmdb"):
        badges += (f'<a class="b tm" href="https://www.themoviedb.org/movie/{m["tmdb_id"]}" target="_blank" title="View on TMDB">TMDB {m["tmdb"]}</a>'
                   if m.get("tmdb_id") else f'<span class="b tm" title="TMDB user score">TMDB {m["tmdb"]}</span>')
    if m.get("au"):
        badges += f'<span class="b au" title="Australian classification">{html.escape(m["au"].replace(" ", ""))}</span>'
    if m.get("has_sub"):
        badges += '<span class="b sub" title="Chinese subtitle available">SUB</span>'
    # Falls back to the release date's year, and finally "" - never None, which Python would
    # otherwise interpolate into the HTML below as the literal text "None", which the year filter
    # dropdown would then offer as if it were a real year.
    year = m.get("year") or (m.get("released") or "")[:4] or ""
    rt_min = int(m["runtime"]) if str(m.get("runtime") or "").isdigit() else None
    runtime = f"⏱ {rt_min // 60}h {rt_min % 60:02d}m" if rt_min and rt_min >= 60 else f"⏱ {rt_min} min" if rt_min else ""
    meta = " · ".join(filter(None, [m.get("released") or year, runtime, m.get("country")]))
    genres = "".join(f'<span class="g">{html.escape(g)}</span>' for g in m["genres"])
    # A discover-only entry (no NAS path) gets a "Not in library" ribbon, so it reads clearly as a
    # suggestion rather than something you already own.
    badge_html = "" if m.get("path") else '<span class="discover-badge">Not in library</span>'
    return (f'<div class="card" data-t="{html.escape(m["name"].lower())}" data-imdb="{m.get("imdb") or 0}" '
            f'data-rt="{(m.get("rt") or "0").rstrip("%")}" data-tmdb="{m.get("tmdb") or 0}" data-y="{year}" data-d="{m.get("released") or year + "-00-00"}">'
            f'{poster_tag}<div class="body">{badge_html}<h2>{html.escape(m["name"])}</h2>'
            f'<p class="meta">{html.escape(meta)}</p><div class="genres">{genres}</div><div class="badges">{badges}</div>'
            f'<p class="intro">{html.escape(m["overview"])}</p>'
            + (f'<p class="path">{html.escape(m["path"])}</p>' if m.get("path") else "") + '</div></div>')


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Movies</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%237c3aed'/%3E%3Cstop offset='1' stop-color='%23f97316'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url%28%23g%29'/%3E%3Crect x='6' y='27' width='52' height='30' rx='4' fill='%23fff'/%3E%3Crect x='6' y='11' width='52' height='13' rx='3' fill='%231f2937'/%3E%3Cpolygon points='12%2C11 22%2C11 16%2C24 6%2C24' fill='%23facc15'/%3E%3Cpolygon points='26%2C11 36%2C11 30%2C24 20%2C24' fill='%2322d3ee'/%3E%3Cpolygon points='40%2C11 50%2C11 44%2C24 34%2C24' fill='%23f43f5e'/%3E%3Cpolygon points='54%2C11 58%2C11 58%2C24 48%2C24' fill='%234ade80'/%3E%3Cpolygon points='26%2C33 26%2C52 44%2C42.5' fill='%23f43f5e'/%3E%3C/svg%3E"><style>
:root{--bg:#fff;--fg:#1a1a1a;--card:#f4f4f6;--mut:#6b6b76}
@media(prefers-color-scheme:dark){:root{--bg:#141417;--fg:#eee;--card:#1f1f24;--mut:#9a9aa6}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;z-index:1;border-bottom:1px solid var(--card)}
header h1{font-size:18px;margin:0 8px 0 0}input,select{padding:6px 10px;font:inherit;border-radius:6px;border:1px solid var(--mut);background:var(--card);color:var(--fg)}
main,.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:16px;padding:16px}
.card{display:flex;gap:14px;background:var(--card);border-radius:10px;overflow:hidden}
.poster{display:block;flex:0 0 240px;min-height:360px;background:#0002}.poster img{width:240px;height:100%;object-fit:cover;display:block}
.body{padding:12px 12px 12px 0;min-width:0;position:relative}h2{margin:0;font-size:17px}.meta,.path{margin:2px 0;color:var(--mut);font-size:13px}
.path{font-size:11px;word-break:break-all}.intro{margin:8px 0;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.genres{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}.g{font-size:11px;padding:1px 8px;border-radius:10px;border:1px solid var(--mut);color:var(--mut)}.badges{display:flex;gap:6px;margin-top:6px}.b{font-size:12px;font-weight:600;padding:2px 8px;border-radius:5px;text-decoration:none}
.imdb{background:#f5c518;color:#000}.rt{background:#fa320a;color:#fff}.tm{background:#0369a1;color:#fff}.au{background:#0b6e4f;color:#fff}.sub{background:#64748b;color:#fff}.miss{opacity:.6}
@media(max-width:600px){main,.grid{grid-template-columns:1fr}.poster,.poster img{flex-basis:150px;width:150px}}
#disc{display:none;margin-top:20px}
#disc h3{margin:0;padding:20px 16px;font-size:48px;color:var(--fg);font-weight:700;background:#7c3aed88}
#disc .grid{display:none}
.discover-badge{position:absolute;top:8px;right:12px;background:#7c3aed;color:#fff;font-size:10px;font-weight:700;
  padding:2px 8px;border-radius:10px;letter-spacing:.03em}
.body:has(.discover-badge) h2{padding-right:88px}
</style>
<header><h1 id="count">Movies (__N__)</h1><input id="q" placeholder="Search…"><select id="s">
<option value="imdb" selected>IMDb score</option><option value="t">Name (A–Z)</option><option value="d">Release date (newest)</option><option value="rt">Rotten Tomatoes</option><option value="tmdb">TMDB score</option></select><select id="yr"></select></header>
<main id="m">__CARDS__</main>
<div id="disc"><h3 id="discH"></h3>__DISCOVER_GRIDS__</div>
<script>
const m=document.getElementById('m'),cards=[...m.children];
const discGrids=[...document.querySelectorAll('#disc .grid')];
function renderDiscover(y){
  const active=discGrids.find(g=>g.dataset.y===y&&g.children.length);
  discGrids.forEach(g=>g.style.display=g===active?'grid':'none');
  document.getElementById('disc').style.display=active?'block':'none';
  document.getElementById('discH').textContent=`Best Movies of ${y}`;
}
const yr=document.getElementById('yr'),years=[...new Set(cards.map(c=>c.dataset.y).filter(Boolean))].sort().reverse();
yr.innerHTML='<option value="">All years</option>'+years.map(y=>`<option>${y}</option>`).join('');
const cur=String(new Date().getFullYear());yr.value=years.includes(cur)?cur:'';
function go(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value,y=yr.value;
let shown=0;
cards.forEach(c=>{const visible=(c.dataset.t||c.textContent.toLowerCase()).includes(q)&&(!y||c.dataset.y===y);c.style.display=visible?'':'none';if(visible)shown++});
[...cards].sort((a,b)=>s=='t'?(a.dataset.t||'~').localeCompare(b.dataset.t||'~'):s=='d'?(b.dataset.d||'').localeCompare(a.dataset.d||''):(+b.dataset[s]||0)-(+a.dataset[s]||0)).forEach(c=>m.appendChild(c));
document.getElementById('count').textContent=shown===cards.length?`Movies (${cards.length})`:`Movies (${shown} of ${cards.length})`;
renderDiscover(y)}
// Searching for a specific movie shouldn't also require remembering to switch to "All years" first,
// and picking a year is a fresh browse that shouldn't still be narrowed by an old search term.
const qInput=document.getElementById('q');
qInput.oninput=()=>{if(qInput.value)yr.value='';go()};
yr.onchange=()=>{if(qInput.value)qInput.value='';go()};
s.onchange=go;go();
</script>"""


def discover_year(year, exclude_imdb_ids, limit=10, max_checked=40):
    """Highly rated (real IMDb score > 7.5, via OMDb - more reputable than TMDB's own user score)
    movies for `year` that aren't already in the library. Each is a full card row, same shape as a
    library movie, minus a NAS "path". Candidates are still pulled from TMDB sorted by its own vote
    average - a fine proxy ordering to check the most-likely-to-qualify titles first - but the actual
    7.5 cutoff is applied to the real IMDb rating."""
    global OMDB_DOWN
    out, checked = [], 0
    for page in (1, 2):
        if len(out) >= limit or checked >= max_checked or OMDB_DOWN:
            break
        try:
            results = tmdb("/discover/movie", primary_release_year=year, sort_by="vote_average.desc",
                            page=page, **{"vote_count.gte": 1000})["results"]
        except Exception:
            break
        for d in results:
            if len(out) >= limit or checked >= max_checked or OMDB_DOWN:
                break
            checked += 1
            try:
                row = tmdb_details({"path": None}, d["id"], "en-US")
            except Exception:
                continue
            if row.get("imdb_id") in exclude_imdb_ids:
                continue
            imdb_score = None
            try:
                o = get(f"https://www.omdbapi.com/?apikey={OMDB}&i={row['imdb_id']}")
                imdb_score = None if o.get("imdbRating", "N/A") == "N/A" else o["imdbRating"]
            except urllib.error.HTTPError as ex:
                OMDB_DOWN = OMDB_DOWN or ex.code == 401
                continue
            except Exception:
                continue
            if not imdb_score or float(imdb_score) <= 7.5:
                continue
            row["imdb"], row["scored"] = imdb_score, True
            out.append(row)
    out.sort(key=lambda r: -float(r["imdb"]))
    return out


def build_discover(cache, rows):
    """{"2023": [...]} of highly rated movies per year not already in the library. Cached forever per
    year - so a year is only ever computed once OMDb is actually up, never left cached with too few
    results just because the day's OMDb quota ran out partway through checking it (discover_year
    needs a real IMDb rating per candidate, unlike the rest of the script, which can leave a movie's
    score to fill in on a later run without dropping the movie itself)."""
    if not TMDB:
        return json.loads(DISCOVER_CACHE.read_text()) if DISCOVER_CACHE.exists() else {}
    dcache = json.loads(DISCOVER_CACHE.read_text()) if DISCOVER_CACHE.exists() else {}
    # Only movies currently on the NAS (rows, from this run's scan()) - not every entry ever cached,
    # which never gets pruned when a movie is deleted, so a deleted movie's imdb_id would otherwise
    # stay "owned" forever and never come back to Discover.
    lib_imdb_ids = {r.get("imdb_id") for r in rows if r.get("found") and r.get("imdb_id")}
    years = sorted({r["year"] for r in rows if r.get("year")}, reverse=True)[:20]
    new_years = [y for y in years if y not in dcache]
    if new_years and not OMDB:
        print("skipping discover: needs OMDB_API_KEY (for real IMDb scores)")
    elif new_years:
        print(f"finding highly rated movies for {len(new_years)} years you're missing")
    for y in new_years:
        if OMDB_DOWN:
            print(f"  OMDb daily limit reached; {y} and later years will be tried on a later run")
            break
        if not OMDB:
            break
        dcache[y] = discover_year(int(y), lib_imdb_ids)
    stale = [m for y in dcache for m in dcache[y] if m.get("imdb_id") and "country" not in m]
    if stale:  # cached before the TMDB id / country were added: fill them in (one call each, since tmdb_id is often already known)
        print(f"adding TMDB id/country to {len(stale)} discovered movies")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(backfill_au, stale))
    if OMDB and not OMDB_DOWN:  # opportunistic: add the real IMDb score where we can, without blocking on it
        unscored = [m for y in dcache for m in dcache[y] if m.get("imdb_id") and not m.get("scored")]
        if unscored:
            print(f"adding IMDb scores to {len(unscored)} discovered movies")
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(backfill_scores, unscored))
    DISCOVER_CACHE.write_text(json.dumps(dcache, ensure_ascii=False, indent=1))
    # dcache on disk keeps every movie ever discovered, even ones you've since added to the library -
    # so if one is later removed from the library again, it reappears here instead of needing a
    # rebuild of the whole (rate-limited) discover cache. Only what's actually shown/exported is
    # filtered against the *current* library, fresh on every run.
    return {y: [m for m in ms if m.get("imdb_id") not in lib_imdb_ids] for y, ms in dcache.items()}


def abs_path_for(catalog_path):
    """"movies/2026/Foo" -> /Volumes/movies/2026/Foo, resolved against the actual ROOTS used for this
    run rather than assuming /Volumes, in case build.py was pointed at custom folders."""
    root_name = catalog_path.split("/", 1)[0]
    for root in ROOTS:
        if root.name == root_name:
            return root.parent / catalog_path
    return Path("/Volumes") / catalog_path


def video_and_dir(path):
    """(video file, its containing dir, has its own folder) for a movie's absolute path - the video
    is the largest video file inside if `path` is a folder, or `path` itself if it's a bare file
    (sharing its parent folder with other movies). video is None if nothing playable is found."""
    if path.is_dir():
        videos = [f for f in path.iterdir() if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in VIDEO]
        video = max(videos, key=lambda f: f.stat().st_size) if videos else None
        return video, path, True
    if path.is_file():
        return path, path.parent, False
    return None, path, False


def has_subtitle(video, dir_path, own_folder):
    """Whether a subtitle already sits next to `video` - anywhere in `dir_path` for a one-movie-per-
    folder layout, or matching `video`'s own name for a bare file sharing a folder with other movies."""
    if not dir_path.is_dir():
        return False
    for f in dir_path.iterdir():
        if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in SUBTITLE_EXT:
            continue
        if own_folder or f.stem.startswith(video.stem):
            return True
    return False


OPENSUBTITLES_DOWN = False


def download_chinese_subtitle(imdb_id):
    """The most-downloaded Chinese (Simplified) subtitle for an IMDb id, via the OpenSubtitles REST
    API, as bytes - or None if there's no match, the API key is missing/exhausted, or anything else
    goes wrong. Stops trying for the rest of this run once the API reports its quota is used up."""
    global OPENSUBTITLES_DOWN
    if not OPENSUBTITLES or OPENSUBTITLES_DOWN or not imdb_id:
        return None
    # Without an explicit Accept header, the download endpoint returns a 503 - it's the site's
    # generic HTML error page, not an actual server error.
    headers = {"Api-Key": OPENSUBTITLES, "User-Agent": "movie-catalog/1.0", "Content-Type": "application/json", "Accept": "application/json"}
    numeric_id = imdb_id.lstrip("t")
    try:
        req = urllib.request.Request(
            "https://api.opensubtitles.com/api/v1/subtitles?"
            + urllib.parse.urlencode({"imdb_id": numeric_id, "languages": "zh-cn", "order_by": "download_count"}),
            headers=headers,
        )
        results = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("data", [])
        if not results:
            return None
        files = results[0].get("attributes", {}).get("files", [])
        if not files:
            return None
        file_id = files[0]["file_id"]
        req = urllib.request.Request(
            "https://api.opensubtitles.com/api/v1/download",
            data=json.dumps({"file_id": file_id}).encode(),
            headers=headers,
            method="POST",
        )
        info = json.loads(urllib.request.urlopen(req, timeout=20).read())
        link = info.get("link")
        if not link:
            return None
        # The actual file host (not the API itself) rejects the app-identifying User-Agent above with
        # a 403 - it wants an ordinary browser-looking one.
        file_req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(file_req, timeout=30).read()
    except urllib.error.HTTPError as ex:
        OPENSUBTITLES_DOWN = OPENSUBTITLES_DOWN or ex.code in (401, 403, 406, 429)
        return None
    except Exception:
        return None


def annotate_subtitle_flag(rows):
    """Sets `has_sub` on every found row, for the "SUB" badge - a real filesystem check, not just
    "did the download step above just fetch one", so it also catches subtitles you already had."""
    for r in rows:
        if not r.get("found") or not r.get("path"):
            r["has_sub"] = False
            continue
        video, dir_path, own_folder = video_and_dir(abs_path_for(r["path"]))
        r["has_sub"] = bool(video and has_subtitle(video, dir_path, own_folder))


def download_missing_subtitles(rows):
    """Chinese subtitles for this year's movies that don't already have one - old movies are assumed
    to already have subtitles, so only the current year is worth the (rate-limited) API calls."""
    if not OPENSUBTITLES:
        return
    current_year = str(datetime.now().year)
    candidates = []
    for r in rows:
        if r.get("year") != current_year or not r.get("found") or not r.get("path") or not r.get("imdb_id"):
            continue
        video, dir_path, own_folder = video_and_dir(abs_path_for(r["path"]))
        if video and not has_subtitle(video, dir_path, own_folder):
            candidates.append((r, video))
    if not candidates:
        return
    print(f"looking for Chinese subtitles for {len(candidates)} of this year's movies")
    added = 0
    for r, video in candidates:
        if OPENSUBTITLES_DOWN:
            print("  OpenSubtitles quota used up; the rest will be tried on a later run")
            break
        content = download_chinese_subtitle(r["imdb_id"])
        if content:
            dest = video.parent / f"{video.stem}.chi.srt"
            dest.write_bytes(content)
            added += 1
    if added:
        print(f"  saved {added} subtitle(s)")


def main():
    if not OMDB:
        sys.exit("Set OMDB_API_KEY in env or .env")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    # older caches keyed paths relative to /Volumes/movies
    if any(not k.startswith(("movies/", "movies-2T/")) for k in cache):
        cache = {("movies/" + k): dict(v, path="movies/" + v["path"]) for k, v in cache.items()}
    # older caches may have "year" as a JSON number instead of a string (from before scan()/
    # tmdb_lookup/omdb_lookup were made consistent about it) - fix those in place so build_discover's
    # sort() of years doesn't fail comparing a mix of str and int.
    for v in cache.values():
        if isinstance(v.get("year"), int):
            v["year"] = str(v["year"])
    # even older cache entries have "year": null despite having a real "released" date (from before
    # tmdb_details/omdb_lookup always set it) - card()'s data-y ends up with the literal text "None"
    # for these, which the year filter dropdown then shows as a bogus selectable "year" of its own.
    for v in cache.values():
        if not v.get("year") and v.get("released"):
            v["year"] = v["released"][:4]
    movies = scan()
    todo = [m for m in movies if m["path"] not in cache
            or not cache[m["path"]].get("found") and ("error" in cache[m["path"]] or cache[m["path"]]["title"] != m["title"]
                                                     or TMDB and not cache[m["path"]].get("tried"))
            or cache[m["path"]].get("ov") != m.get("ov")]
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
    todo_scores = [c for c in cache.values() if c.get("src") == "tmdb" and c.get("imdb_id") and not c.get("scored")]
    if todo_scores and OMDB:
        print(f"adding IMDb/RT scores to {len(todo_scores)} TMDB-sourced movies")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(backfill_scores, todo_scores))
        if OMDB_DOWN:
            print("  OMDb daily limit reached; scores will be added on a later run")
    if TMDB:
        need = [c for c in cache.values() if c.get("imdb_id") and ("au" not in c or "tmdb" not in c or "tmdb_id" not in c or "country" not in c)]
        if need:
            print(f"adding Australian classification and TMDB score to {len(need)} movies")
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(backfill_au, need))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    rows = [cache[m["path"]] for m in movies]
    rows.sort(key=lambda r: (r.get("name") or r["title"]).lower())
    download_missing_subtitles(rows)
    annotate_subtitle_flag(rows)
    discover = build_discover(cache, rows)
    disc_grids = "".join(f'<div class="grid" data-y="{html.escape(y)}">{"".join(map(card, movies))}</div>'
                          for y, movies in discover.items())
    (HERE / "movies.html").write_text(PAGE.replace("__N__", str(len(rows)))
                                       .replace("__CARDS__", "".join(map(card, rows)))
                                       .replace("__DISCOVER_GRIDS__", disc_grids))
    write_json(rows, discover)
    misses = [r for r in rows if not r.get("found")]
    print(f"unmatched: {len(misses)}  -> {HERE/'movies.html'}")
    for m in misses:
        print(f"  {m['path']}")


def slim(m):
    """A movie row without the internal/heavy fields (_tmdb, ov) - for movies.json, read by the Android TV app."""
    return {k: v for k, v in m.items() if k not in ("_tmdb", "ov", "error", "tried", "scored", "src")}


def write_json(rows, discover):
    """movies.json: same data as movies.html, for the Android TV app. Also dropped into each NAS root's
    movie-catalog/ folder (if present) so the app can read it over SMB, the same way it reads video files."""
    catalog = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "movies": [slim(r) for r in rows if r.get("found")],
        "discover": {y: [slim(m) for m in ms] for y, ms in discover.items()},
    }
    data = json.dumps(catalog, ensure_ascii=False)
    (HERE / "movies.json").write_text(data)
    for root in ROOTS:
        dest = root / "movie-catalog" / "movies.json"
        if dest.parent.is_dir():
            dest.write_text(data)
            print(f"  copied movies.json -> {dest}")


if __name__ == "__main__":
    main()
