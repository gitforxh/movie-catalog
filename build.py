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
SKIP_DIRS = {"TV", "tmp", "TVseries", "upload", "4k"}
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts"}
CACHE = HERE / "cache.json"
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
                        found[p] = {"title": title, "year": year, "path": p, "ov": ov}
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


def backfill_au(m):
    """Add the Australian classification (m["au"], "" if none) via TMDB, by IMDb id."""
    try:
        found = tmdb(f"/find/{m['imdb_id']}", external_source="imdb_id")["movie_results"]
        au = ""
        if found:
            m["tmdb"] = tmdb_score(found[0])
            for c in tmdb(f"/movie/{found[0]['id']}/release_dates")["results"]:
                if c["iso_3166_1"] == "AU":
                    au = next((d["certification"] for d in c["release_dates"] if d["certification"]), "")
        m["au"] = au
        m.setdefault("tmdb", "")
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
        imdb=None, rt=None, au=au, tmdb=tmdb_score(d),
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


def lookup(m):
    global OMDB_DOWN
    ov = m.get("ov") or {}
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
    )
    return out


def link(m):
    return "file://" + urllib.parse.quote(str(ROOTS[0].parent / m["path"]))


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
    if m.get("tmdb"):
        badges += f'<span class="b tm" title="TMDB user score">TMDB {m["tmdb"]}</span>'
    if m.get("au"):
        badges += f'<span class="b au" title="Australian classification">{html.escape(m["au"].replace(" ", ""))}</span>'
    rt_min = int(m["runtime"]) if str(m.get("runtime") or "").isdigit() else None
    runtime = f"⏱ {rt_min // 60}h {rt_min % 60:02d}m" if rt_min and rt_min >= 60 else f"⏱ {rt_min} min" if rt_min else ""
    meta = " · ".join(filter(None, [m.get("released") or m["year"], runtime]))
    genres = "".join(f'<span class="g">{html.escape(g)}</span>' for g in m["genres"])
    return (f'<div class="card" data-t="{html.escape(m["name"].lower())}" data-imdb="{m.get("imdb") or 0}" '
            f'data-rt="{(m.get("rt") or "0").rstrip("%")}" data-tmdb="{m.get("tmdb") or 0}" data-y="{m["year"]}" data-d="{m.get("released") or m["year"] + "-00-00"}">'
            f'<a class="poster" href="{link(m)}" target="_blank" title="Open folder">{poster}</a><div class="body"><h2>{html.escape(m["name"])}</h2>'
            f'<p class="meta">{html.escape(meta)}</p><div class="genres">{genres}</div><div class="badges">{badges}</div>'
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
.genres{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}.g{font-size:11px;padding:1px 8px;border-radius:10px;border:1px solid var(--mut);color:var(--mut)}.badges{display:flex;gap:6px;margin-top:6px}.b{font-size:12px;font-weight:600;padding:2px 8px;border-radius:5px;text-decoration:none}
.imdb{background:#f5c518;color:#000}.rt{background:#fa320a;color:#fff}.tm{background:#0369a1;color:#fff}.au{background:#0b6e4f;color:#fff}.miss{opacity:.6}
@media(max-width:600px){main{grid-template-columns:1fr}.poster,.poster img{flex-basis:150px;width:150px}}
</style>
<header><h1>Movies (__N__)</h1><input id="q" placeholder="Search…"><select id="s">
<option value="imdb" selected>IMDb score</option><option value="t">Name (A–Z)</option><option value="d">Release date (newest)</option><option value="rt">Rotten Tomatoes</option><option value="tmdb">TMDB score</option></select><select id="yr"></select></header>
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
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    # older caches keyed paths relative to /Volumes/movies
    if any(not k.startswith(("movies/", "movies-2T/")) for k in cache):
        cache = {("movies/" + k): dict(v, path="movies/" + v["path"]) for k, v in cache.items()}
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
        need = [c for c in cache.values() if c.get("imdb_id") and ("au" not in c or "tmdb" not in c)]
        if need:
            print(f"adding Australian classification and TMDB score to {len(need)} movies")
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(backfill_au, need))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    rows = [cache[m["path"]] for m in movies]
    rows.sort(key=lambda r: (r.get("name") or r["title"]).lower())
    (HERE / "movies.html").write_text(PAGE.replace("__N__", str(len(rows))).replace("__CARDS__", "".join(map(card, rows))))
    print(f"unmatched: {sum(not r.get('found') for r in rows)}  -> {HERE/'movies.html'}")


if __name__ == "__main__":
    main()
