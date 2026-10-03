#!/usr/bin/env python3
"""Scan movie folders, look up their metadata and write movies.html and movies.json.

Usage: python3 build.py [options] [folder ...]

With no options it does a full run: scan the folders, look up new movies, retry scores that are
still pending, find highly rated movies you don't have (IMDb > 7.5), and download missing Chinese
(else English) subtitles for this year's movies.

Options:
  -g          Regenerate movies.html from the existing movies.json only: no folder scan, no network.
  -n          Scan for new movies only: look up what's new on disk, but don't re-query scores for
              existing movies, and don't discover new ones.
  -s YEAR     Check/download subtitles for that year's movies only, and update their CN/EN badge.
              Doesn't scan the folders; needs an existing movies.json.
  -h, --help  Show this help.

  folder ...  Movie folders to scan instead of the defaults (/Volumes/movies, /Volumes/movies-2T).
              If options are combined, -g wins over -s, which wins over -n.

Examples:
  python3 build.py                     full run
  python3 build.py -n                  just pick up new movies
  python3 build.py -s 2024             subtitles for 2024 only
  python3 build.py /path/to/movies     scan your own folder

Keys, in the environment or in ./.env: OMDB_API_KEY (required; -g and -s don't need it),
TMDB_API_KEY, OPENSUBTITLES_API_KEY, and TORRENT_SEARCH_URL (all optional).
Results are cached in cache.json, so re-runs only look up what's new or changed.
"""
from datetime import datetime, timedelta
import html, json, os, re, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
    print(__doc__.strip())
    sys.exit(0)
GENERATE_ONLY = "-g" in sys.argv[1:]
SCAN_ONLY = "-n" in sys.argv[1:]
SUBTITLE_YEAR = None
_cli_args = list(sys.argv[1:])
if "-s" in _cli_args:
    _subtitle_index = _cli_args.index("-s")
    if _subtitle_index + 1 >= len(_cli_args) or not re.fullmatch(r"(?:19|20)\d{2}", _cli_args[_subtitle_index + 1]):
        sys.exit("-s needs a year, e.g. -s 2024 (see -h for all options)")
    SUBTITLE_YEAR = _cli_args[_subtitle_index + 1]
    del _cli_args[_subtitle_index:_subtitle_index + 2]
_unknown = [a for a in _cli_args if a.startswith("-") and a not in {"-g", "-n"}]
if _unknown:  # otherwise a typo like "-x" would quietly be taken for a folder to scan
    sys.exit(f"Unknown option {_unknown[0]} (see -h for the options)")
CLI_ROOTS = [p for p in _cli_args if p not in {"-g", "-n"}]
ROOTS = [Path(p) for p in CLI_ROOTS] or [Path("/Volumes/movies"), Path("/Volumes/movies-2T")]
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
TORRENT_SEARCH_URL = ENV.get("TORRENT_SEARCH_URL", "https://www.google.com/search?q={query}")
SUBTITLE_EXT = {".srt", ".vtt", ".ass", ".ssa"}

JUNK = re.compile(
    r"\b(3d|hsbs|half-?sbs|sbs|3dtv|hc|2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|brrip|bdrip|web-?dl|web-?rip|webrip|hdrip|hdtv|"
    r"dvdrip|x26[45]|h\.?26[45]|hevc|10bit|8bit|aac\S*|ddp\S*|dts\S*|atmos|5\.1|7\.1|extended|"
    r"remastered|imax|repack|proper|dual|amzn|nf|itunes|yts|yify|rarbg)\b.*",
    re.I,
)


CJK = re.compile(r"[\u4e00-\u9fff]")

# TMDB returns genre names localized to whatever language a request used, so a Chinese-titled movie
# (looked up with language=zh-CN) gets Chinese genre names while everything else gets English -
# splitting what should be one genre (e.g. "Drama"/"\u5267\u60c5") into two separate, unmergeable entries in
# the genre filter. Normalize every genre name to TMDB's own English ones (its full standard movie
# genre list) as soon as it's fetched, so movies.json/cache.json only ever store one canonical name.
GENRE_ZH_TO_EN = {
    "\u52a8\u4f5c": "Action", "\u5192\u9669": "Adventure", "\u52a8\u753b": "Animation", "\u559c\u5267": "Comedy", "\u72af\u7f6a": "Crime",
    "\u7eaa\u5f55": "Documentary", "\u7eaa\u5f55\u7247": "Documentary", "\u5267\u60c5": "Drama", "\u5bb6\u5ead": "Family", "\u5947\u5e7b": "Fantasy",
    "\u5386\u53f2": "History", "\u6050\u6016": "Horror", "\u97f3\u4e50": "Music", "\u60ac\u7591": "Mystery", "\u7231\u60c5": "Romance",
    "\u79d1\u5e7b": "Science Fiction", "\u7535\u89c6\u7535\u5f71": "TV Movie", "\u60ca\u609a": "Thriller", "\u6218\u4e89": "War", "\u897f\u90e8": "Western",
}


def normalize_genres(names):
    return [GENRE_ZH_TO_EN.get(g, g) for g in names]
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
            print(f"[scan] directory: {d}")
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
    safe = re.sub(r"([?&]apikey=)[^&]+", r"\1***", url)
    print(f"[http] GET {safe}")
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
    print(f"[TMDB] {path} {params}")
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
        if res:
            return tmdb_details(out, res[0]["id"], lang)
        return omdb_lookup_by_id(out, ov["imdb"]) or out
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
        genres=normalize_genres(g["name"] for g in d.get("genres", [])),
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
    year_label = f" ({m['year']})" if m.get("year") else ""
    print(f"[metadata] looking up: {m['title']}{year_label}")
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
            print(f"[OMDb] result for {m['title']}: {'found' if out.get('found') else 'no match'}")
            if out.get("found") or not TMDB:
                return out
    except Exception as ex:
        OMDB_DOWN = OMDB_DOWN or getattr(ex, "code", None) == 401
        if not TMDB:
            return dict(m, found=False, error=str(ex))
    try:  # OMDb failed (daily limit, or no match): try TMDB
        out = tmdb_lookup(m)
        print(f"[TMDB] result for {m['title']}: {'found' if out.get('found') else 'no match'}")
        return out
    except Exception as ex:
        return dict(m, found=False, error=str(ex))


def parse_omdb_response(out, o):
    na = lambda v: None if v in (None, "N/A") else v
    rt = next((r["Value"] for r in o.get("Ratings", []) if r["Source"] == "Rotten Tomatoes"), None)
    out.update(
        found=True,
        name=o["Title"],
        year=(na(o.get("Year")) or "")[:4],  # a series' "Year" can be a range like "2021-2024"
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
    return parse_omdb_response(out, o)


def omdb_lookup_by_id(out, imdb_id):
    """OMDb's record for a known IMDb id, regardless of media type - unlike the title-search path
    above, a direct id lookup isn't restricted to type=movie. Used as a fallback for a title that
    exists on IMDb but has no *movie* entry on TMDB: usually a TV special or episode (or, sometimes,
    a whole series saved as a single file) that got mixed into a movie folder."""
    if not OMDB:
        return None
    try:
        o = get(f"https://www.omdbapi.com/?apikey={OMDB}&i={imdb_id}&plot=short")
    except Exception:
        return None
    if o.get("Response") == "False":
        return None
    row = parse_omdb_response(out, o)
    row["media_type"] = o.get("Type")  # "movie" | "series" | "episode" - handy to know, not shown anywhere yet
    return row


def link(m):
    return "file://" + urllib.parse.quote(str(ROOTS[0].parent / m["path"]))


def torrent_link(m):
    """Return a configurable search URL for a discovered movie."""
    title = m.get("name") or m.get("title") or ""
    year = m.get("year") or (m.get("released") or "")[:4]
    query = urllib.parse.quote_plus(f'"{title}" {year}'.strip())
    try:
        return TORRENT_SEARCH_URL.format(query=query, title=urllib.parse.quote_plus(title), year=year)
    except (KeyError, ValueError):
        return "https://www.google.com/search?q=" + query


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
    sub = sub_badge(m)
    if sub:
        badges += f'<span class="b sub" title="{"Chinese" if sub == "CN" else "English"} subtitle available">{sub}</span>'
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
    badge_html = ("" if m.get("path") else
                  f'<a class="torrent discover-torrent" href="{html.escape(torrent_link(m), quote=True)}" target="_blank" rel="noopener">Torrents</a>')
    return (f'<div class="card" data-t="{html.escape(m["name"].lower())}" data-imdb="{m.get("imdb") or 0}" '
            f'data-rt="{(m.get("rt") or "0").rstrip("%")}" data-tmdb="{m.get("tmdb") or 0}" data-y="{year}" data-d="{m.get("released") or year + "-00-00"}" '
            f'data-c="{html.escape(m.get("country") or "")}" data-g="{html.escape(", ".join(m["genres"]))}">'
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
.imdb{background:#f5c518;color:#000}.rt{background:#fa320a;color:#fff}.tm{background:#0369a1;color:#fff}.au{background:#0b6e4f;color:#fff}.sub{background:#64748b;color:#fff}.torrent{display:block;width:max-content;background:#7c3aed;color:#fff;font-size:12px;font-weight:700;padding:2px 10px;border-radius:10px;letter-spacing:.03em;text-decoration:none}.discover-torrent{position:absolute;top:8px;right:12px}.miss{opacity:.6}
@media(max-width:600px){main,.grid{grid-template-columns:1fr}.poster,.poster img{flex-basis:150px;width:150px}}
#disc{display:none;margin-top:20px}
#disc h3{margin:0;padding:20px 16px;font-size:48px;color:var(--fg);font-weight:700;background:#7c3aed88}
#disc .grid{display:none}
.discover-badge{position:absolute;top:8px;right:12px;background:#7c3aed;color:#fff;font-size:10px;font-weight:700;
  padding:2px 8px;border-radius:10px;letter-spacing:.03em}
.body:has(.discover-torrent) h2{padding-right:88px}
</style>
<header><h1 id="count">Movies (__N__)</h1><input id="q" placeholder="Search…"><select id="s">
<option value="imdb" selected>IMDb score</option><option value="t">Name (A–Z)</option><option value="d">Release date (newest)</option><option value="rt">Rotten Tomatoes</option><option value="tmdb">TMDB score</option></select><select id="yr"></select><select id="ct"></select><select id="gn"></select></header>
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
// A movie can list several countries (e.g. co-productions) in one comma-separated field - split
// those out so each one gets its own dropdown entry and matches on its own.
const ct=document.getElementById('ct'),countries=[...new Set(cards.flatMap(c=>(c.dataset.c||'').split(',').map(x=>x.trim()).filter(Boolean)))].sort();
ct.innerHTML='<option value="">All countries</option>'+countries.map(c=>`<option>${c}</option>`).join('');
const gn=document.getElementById('gn'),genres=[...new Set(cards.flatMap(c=>(c.dataset.g||'').split(',').map(x=>x.trim()).filter(Boolean)))].sort();
gn.innerHTML='<option value="">All genres</option>'+genres.map(g=>`<option>${g}</option>`).join('');
function go(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value,y=yr.value,c2=ct.value,g2=gn.value;
let shown=0;
cards.forEach(c=>{const visible=(c.dataset.t||c.textContent.toLowerCase()).includes(q)&&(!y||c.dataset.y===y)&&(!c2||(c.dataset.c||'').split(',').map(x=>x.trim()).includes(c2))&&(!g2||(c.dataset.g||'').split(',').map(x=>x.trim()).includes(g2));c.style.display=visible?'':'none';if(visible)shown++});
[...cards].sort((a,b)=>s=='t'?(a.dataset.t||'~').localeCompare(b.dataset.t||'~'):s=='d'?(b.dataset.d||'').localeCompare(a.dataset.d||''):(+b.dataset[s]||0)-(+a.dataset[s]||0)).forEach(c=>m.appendChild(c));
document.getElementById('count').textContent=shown===cards.length?`Movies (${cards.length})`:`Movies (${shown} of ${cards.length})`;
renderDiscover(y)}
// Searching for a specific movie shouldn't also require remembering to switch to "All years"/"All
// countries"/"All genres" first, and picking any of them is a fresh browse that shouldn't still be
// narrowed by an old search term.
const qInput=document.getElementById('q');
qInput.oninput=()=>{if(qInput.value){yr.value='';ct.value='';gn.value=''}go()};
yr.onchange=()=>{if(qInput.value)qInput.value='';go()};
ct.onchange=()=>{if(qInput.value)qInput.value='';go()};
gn.onchange=()=>{if(qInput.value)qInput.value='';go()};
s.onchange=go;go();
</script>"""


def _discover(params, exclude_imdb_ids, limit, max_checked, known_tmdb_ids):
    """Highly rated (real IMDb score > 7.5, via OMDb - more reputable than TMDB's own user score)
    movies matching a TMDB /discover query, that aren't already in the library. Each is a full card
    row, same shape as a library movie, minus a NAS "path". Candidates are pulled from TMDB sorted by
    its own vote average - a fine proxy ordering to check the most-likely-to-qualify titles first -
    but the actual 7.5 cutoff is applied to the real IMDb rating. `known_tmdb_ids` (already cached)
    are skipped without using up the max_checked budget, so a re-check spends it on candidates it
    hasn't seen rather than re-checking the same top few every time."""
    global OMDB_DOWN
    out, checked = [], 0
    for page in (1, 2):
        if len(out) >= limit or checked >= max_checked or OMDB_DOWN:
            break
        try:
            results = tmdb("/discover/movie", sort_by="vote_average.desc", page=page, **params)["results"]
        except Exception:
            break
        for d in results:
            if len(out) >= limit or checked >= max_checked or OMDB_DOWN:
                break
            if d["id"] in known_tmdb_ids:
                continue
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


def discover_year(year, exclude_imdb_ids, limit=10, max_checked=40):
    return _discover({"primary_release_year": year, "vote_count.gte": 1000}, exclude_imdb_ids, limit, max_checked, ())


# Released within this many days counts as "recent" - a window rather than a calendar year, so it keeps
# working across a year boundary (in January it still covers late last year).
RECENT_DAYS = 180


def discover_recent(exclude_imdb_ids, known_tmdb_ids, limit=10, max_checked=40):
    """Newly qualifying movies from the last RECENT_DAYS days. A lower vote floor than a full year's
    (500, not 1000), since a recent release hasn't had time to collect as many votes."""
    now = datetime.now().date()
    params = {
        "primary_release_date.gte": (now - timedelta(days=RECENT_DAYS)).isoformat(),
        "primary_release_date.lte": now.isoformat(),
        "vote_count.gte": 500,
    }
    return _discover(params, exclude_imdb_ids, limit, max_checked, known_tmdb_ids)


DISCOVER_YEAR_CAP = 20  # most movies kept per year once re-checking can add to it


def build_discover(cache, rows):
    """{"2023": [...]} of highly rated movies per year not already in the library. Cached per year, and
    only added to afterwards (plus a re-check of recent releases, see discover_recent) - so a year
    is only ever first computed once OMDb is actually up, never left cached with too few
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
        print(f"[discover] {y}: {len(dcache[y])} highly rated movie(s) found")
        for m in dcache[y]:
            print(f"  + {_movie_line(m)}")
    # Re-check what's been released recently, across year boundaries, for newly qualifying movies: new
    # releases, and ratings that settle above the cutoff after a title was first looked at. Finds are
    # added to their release year's cached list, never replacing it.
    if OMDB and not OMDB_DOWN:
        known = {m.get("tmdb_id") for ms in dcache.values() for m in ms if m.get("tmdb_id")}
        known_imdb = {m.get("imdb_id") for ms in dcache.values() for m in ms}
        fresh = discover_recent(lib_imdb_ids | known_imdb, known)
        for m in fresh:
            y = m.get("year") or (m.get("released") or "")[:4]
            if y:
                dcache[y] = sorted(dcache.get(y, []) + [m], key=lambda x: -float(x.get("imdb") or 0))[:DISCOVER_YEAR_CAP]
        kept = [m for m in fresh if any(x.get("imdb_id") == m.get("imdb_id")
                                        for x in dcache.get(m.get("year") or (m.get("released") or "")[:4], []))]
        print(f"[discover] last {RECENT_DAYS} days: {len(kept)} new highly rated movie(s)" if kept
              else f"[discover] last {RECENT_DAYS} days: nothing new")
        for m in kept:
            print(f"  + {_movie_line(m)}")
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


def subtitle_files(video, dir_path, own_folder):
    """The subtitle files sitting next to `video` - anywhere in `dir_path` for a one-movie-per-folder
    layout, or matching `video`'s own name for a bare file sharing a folder with other movies."""
    if not dir_path.is_dir():
        return []
    return [f for f in dir_path.iterdir()
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in SUBTITLE_EXT
            and (own_folder or f.stem.startswith(video.stem))]


def _decode_strict(raw, enc):
    for cut in range(4):  # the 64KB read can end in the middle of a multi-byte character
        try:
            return raw[:len(raw) - cut].decode(enc)
        except UnicodeDecodeError:
            pass
    return None


def _unicode_text(raw):
    """The text of a UTF-8 or UTF-16 file, or None if it's neither (so probably a legacy encoding).
    UTF-16 is recognised by its BOM, or - since plenty of subtitle files have none - by the file being
    full of NUL bytes, all on the same side (odd offsets: little-endian, even offsets: big-endian)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return _decode_strict(raw, "utf-16")
    # Checked before UTF-8 on purpose: UTF-16 text made of ASCII is NUL-padded but still *valid* UTF-8.
    even, odd = raw[0::2].count(0), raw[1::2].count(0)
    if (even + odd) > len(raw) * 0.1 and (odd > even * 3 or even > odd * 3):
        text = _decode_strict(raw, "utf-16-le" if odd > even else "utf-16-be")
        if text is not None:
            return text
    return _decode_strict(raw, "utf-8-sig")


def _cjk_stats(text):
    cjk = sum("\u4e00" <= c <= "\u9fff" for c in text)
    kana = sum("\u3040" <= c <= "\u30ff" for c in text)
    return cjk, kana, sum(c.isalpha() for c in text)


def is_chinese_subtitle(path):
    """Whether a subtitle file's text is (at least partly) Chinese, by reading its first 64KB: a
    bilingual Chinese/English file counts, an English-only or Japanese one doesn't. The encoding
    isn't assumed - Chinese subtitles are often GBK/GB18030 or Big5 rather than UTF-8."""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError:
        return False
    text = _unicode_text(raw)
    min_ratio = 0.08
    if text is None:
        # A legacy Chinese encoding. Strict decoding means a Latin-1 French/Spanish file (an accented
        # letter followed by a letter is a valid GBK pair) is rejected rather than read as random
        # Chinese; GB18030 and Big5 can both decode the same bytes, so take whichever reads as more Chinese.
        candidates = [t for t in (_decode_strict(raw, "gb18030"), _decode_strict(raw, "big5")) if t is not None]
        if candidates:
            text = max(candidates, key=lambda t: _cjk_stats(t)[0])
        else:  # a mostly-Chinese file with a few corrupt bytes - only accept it if it's clearly Chinese
            text = raw.decode("gb18030", errors="ignore")
            min_ratio = 0.5
    cjk, kana, letters = _cjk_stats(text)
    return cjk >= 30 and cjk / max(letters, 1) >= min_ratio and kana < cjk * 0.3


# Words that are very common in English but not in French/Spanish/Portuguese/German - which is why
# short ones that other languages share ("a", "do", "me", "he", "no") are left out.
_EN_STOPWORDS = {"the", "you", "and", "to", "of", "is", "that", "it", "in", "what", "this", "for", "we",
                 "not", "are", "have", "my", "your", "with", "she", "was", "be", "i'm", "it's", "don't",
                 "you're", "that's", "i'll", "can't", "there", "they", "but", "just", "know"}


def is_english_subtitle(path):
    """Whether a subtitle file is English, by how much of its text is very common English words
    ("the", "you", "and"...) - which tells it apart from another Latin-script language such as French or
    Spanish, where those words barely occur. Anything that's Chinese is checked for before this."""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError:
        return False
    text = _unicode_text(raw) or raw.decode("cp1252", errors="replace")
    words = re.findall(r"[a-z']+", text.lower())
    return len(words) >= 50 and sum(w in _EN_STOPWORDS for w in words) / len(words) >= 0.15


OPENSUBTITLES_DOWN = False


def download_subtitle(imdb_id, language="zh-cn"):
    """The most-downloaded subtitle in `language` (OpenSubtitles code: "zh-cn" Simplified Chinese, "en"
    English) for an IMDb id, via the OpenSubtitles REST API, as bytes - or None if there's no match, the API key is missing/exhausted, or anything else
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
            + urllib.parse.urlencode({"imdb_id": numeric_id, "languages": language, "order_by": "download_count"}),
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


def _subtitle_state(r):
    """(video file, subtitle status) for one movie - "zh" if it has a Chinese subtitle, else "en" if it
    has an English one, "other" if it has subtitles but only in some other language, None if it has none. Reads its folder and subtitle files
    from the NAS, so it's slow-ish."""
    try:
        video, dir_path, own_folder = video_and_dir(abs_path_for(r["path"]))
        if not video:
            return None, None
        files = subtitle_files(video, dir_path, own_folder)
        if not files:
            return video, None
        if any(is_chinese_subtitle(f) for f in files):
            return video, "zh"
        return video, "en" if any(is_english_subtitle(f) for f in files) else "other"
    except OSError:
        return None, None


def _scores(m):
    """"IMDb 7.4, RT 38%, TMDB 8.6, AU PG" for whichever of those a movie has."""
    parts = [f"IMDb {m['imdb']}" if m.get("imdb") else None, f"RT {m['rt']}" if m.get("rt") else None,
             f"TMDB {m['tmdb']}" if m.get("tmdb") else None, f"AU {m['au']}" if m.get("au") else None]
    return ", ".join(p for p in parts if p) or "no scores yet"


def _movie_line(m):
    year = m.get("year") or (m.get("released") or "")[:4]
    return f"{m.get('name') or m.get('title')}{f' ({year})' if year else ''} - {_scores(m)}"


def _print_names(header, rows):
    """A header line, then one movie per line - a long list on a single line is hard to read."""
    print(header)
    for r in rows:
        print(f"    - {r.get('name') or r.get('title')}")


def sub_badge(r):
    """"CN" / "EN" / None - the SUB badge text. Falls back to the old has_sub flag (Chinese only) for
    rows from before the language was recorded."""
    return r.get("sub") or ("CN" if r.get("has_sub") else None)


def update_subtitles(rows, year=None):
    """Downloads a subtitle for each movie of `year` (default: the current year) that has no Chinese
    one: Chinese if OpenSubtitles has it, otherwise English - unless the movie already has an English
    one, which is then left as it is. Then sets the SUB badge (`sub`: "CN", else "EN", else None) on
    every row in `rows`. Only one year is worth the (rate-limited) download calls - older movies are
    assumed to already have subtitles. Each movie's folder is read from the NAS once, in parallel, and
    that result is reused for the badge afterwards."""
    year = year or str(datetime.now().year)
    on_disk = [r for r in rows if r.get("found") and r.get("path")]
    print(f"[subtitles] reading {len(on_disk)} movie folder(s) on the NAS for subtitle files...")
    state = {}
    labels = {"zh": "Chinese subtitle", "en": "English subtitle", "other": "OTHER LANGUAGE", None: "NO SUBTITLE"}
    with ThreadPoolExecutor(8) as ex:
        for i, (r, st) in enumerate(zip(on_disk, ex.map(_subtitle_state, on_disk)), 1):
            state[r["path"]] = st
            video, lang = st
            # Status first and padded to a fixed width, so a missing one stands out down the list.
            status = "NO VIDEO FILE" if not video else labels[lang]
            print(f"  [{i}/{len(on_disk)}] {status:<16} - {r.get('name') or r.get('title')}")

    n = {"zh": 0, "en": 0, "other": 0, None: 0}
    need, no_id, no_video = [], 0, 0
    in_year = [r for r in on_disk if r.get("year") == year]
    for r in in_year:
        video, lang = state[r["path"]]
        if not video:
            no_video += 1
            continue
        n[lang] += 1
        if lang == "zh":
            continue
        if not r.get("imdb_id"):
            no_id += 1
        else:
            need.append((r, video, lang))
    print(f"[subtitles] {year}: {len(in_year)} movies - {n['zh']} Chinese subtitle, {n['en']} English only, "
          f"{n['other']} other language only, {n[None]} none"
          + (f", {no_video} with no video file found" if no_video else "")
          + (f" ({no_id} can't be looked up: no IMDb id)" if no_id else ""))
    saved_zh, saved_en, not_found, not_tried = [], [], [], []
    if need and not OPENSUBTITLES:
        print("[subtitles] OPENSUBTITLES_API_KEY isn't set: not downloading")
    elif need:
        print(f"[subtitles] downloading subtitles for {len(need)} movie(s) from OpenSubtitles (Chinese, else English)...")
        for r, video, lang in need:
            if OPENSUBTITLES_DOWN:
                not_tried.append(r)
                continue
            content = download_subtitle(r["imdb_id"], "zh-cn")
            if content:
                (video.parent / f"{video.stem}.chi.srt").write_bytes(content)
                saved_zh.append(r)
                continue
            # No Chinese one on OpenSubtitles: fall back to English - but not for a movie that already has
            # an English subtitle, which would just download a second one.
            content = download_subtitle(r["imdb_id"], "en") if lang != "en" and not OPENSUBTITLES_DOWN else None
            if content:
                (video.parent / f"{video.stem}.eng.srt").write_bytes(content)
                saved_en.append(r)
            elif lang == "en":
                not_found.append(r)  # still has its English one; just no Chinese to add
            elif OPENSUBTITLES_DOWN:
                not_tried.append(r)
            else:
                not_found.append(r)
        if saved_zh:
            _print_names(f"  saved Chinese ({len(saved_zh)}):", saved_zh)
        if saved_en:
            _print_names(f"  no Chinese available, saved English ({len(saved_en)}):", saved_en)
        if not_found:
            _print_names(f"  nothing found ({len(not_found)}):", not_found)
        if not_tried:
            _print_names(f"  not tried, OpenSubtitles quota used up ({len(not_tried)}) - will be tried on a later run:", not_tried)

    zh_paths = {r["path"] for r in saved_zh}
    en_paths = {r["path"] for r in saved_en}
    changed = []
    for r in rows:
        before = sub_badge(r)
        lang = state[r["path"]][1] if r.get("found") and r.get("path") else None
        if lang == "zh" or (r.get("path") in zh_paths):
            r["sub"] = "CN"
        elif lang == "en" or (r.get("path") in en_paths):
            r["sub"] = "EN"
        else:
            r["sub"] = None
        r["has_sub"] = r["sub"] is not None  # kept for older app builds, which only know the boolean
        if r["sub"] != before:
            changed.append(r)
    _print_names(f"[subtitles] SUB badges: {len(changed)} changed" + (":" if changed else ""), changed)


def subtitle_year_only(year):
    """-s YEAR: check/download subtitles for one year's movies, touching nothing else. The movies are
    the ones movies.json already lists for that year (so no folder scan and no per-movie disk check of
    the rest of the library); only their SUB badge is updated, in cache.json and movies.json."""
    catalog_file = HERE / "movies.json"
    if not catalog_file.exists():
        sys.exit("-s needs an existing movies.json - run a full scan first")
    print("[init] loading cache.json and movies.json")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    catalog = json.loads(catalog_file.read_text())
    listed = [m for m in catalog.get("movies", []) if str(m.get("year") or "") == year and m.get("path")]
    rows = [cache[m["path"]] for m in listed if m["path"] in cache]
    print(f"[scan] subtitle-only mode: {len(rows)} movie(s) from {year} (the rest of the library isn't touched)")
    update_subtitles(rows, year)
    flags = {r["path"]: r["sub"] for r in rows}
    for m in catalog["movies"]:
        if m.get("path") in flags:
            m["sub"] = flags[m["path"]]
            m["has_sub"] = flags[m["path"]] is not None
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    catalog["generated"] = datetime.now().isoformat(timespec="seconds")
    print("[output] updating movies.json and movies.html")
    publish_json(catalog)
    # Rebuilt from movies.json, so (like -g) it doesn't show unmatched cards - run without -s for those.
    write_html(catalog["movies"], catalog.get("discover", {}))


def main():
    if not OMDB and not SUBTITLE_YEAR and not GENERATE_ONLY:
        sys.exit("Set OMDB_API_KEY in env or .env")
    if GENERATE_ONLY:
        print("[generate] HTML-only mode: using existing movies.json; no scan or network queries")
        catalog = json.loads((HERE / "movies.json").read_text())
        rows = catalog.get("movies", [])
        discover = catalog.get("discover", {})
        write_html(rows, discover)
        print(f"[generate] wrote {HERE / 'movies.html'} ({len(rows)} library, {sum(map(len, discover.values()))} discovered)")
        return
    if SUBTITLE_YEAR:
        subtitle_year_only(SUBTITLE_YEAR)
        return
    if SCAN_ONLY:
        print("[scan] incremental mode: existing movies will not be re-scored or rediscovered")
    print("[init] loading cache.json")
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
    # a "poster" override added/changed after a movie was already cached won't otherwise take effect
    # until something else about it triggers a re-lookup (a poster override alone doesn't).
    for v in cache.values():
        ov_poster = (v.get("ov") or {}).get("poster")
        if ov_poster and v.get("poster") != ov_poster:
            v["poster"] = ov_poster
    # a movie cached before GENRE_ZH_TO_EN existed can still have Chinese genre names in it.
    for v in cache.values():
        if v.get("genres"):
            v["genres"] = normalize_genres(v["genres"])
    print(f"[scan] roots: {', '.join(map(str, ROOTS))}")
    movies = scan()
    print(f"[scan] found {len(movies)} movie entries")
    todo = [m for m in movies if m["path"] not in cache
            or not cache[m["path"]].get("found") and ("error" in cache[m["path"]] or cache[m["path"]]["title"] != m["title"]
                                                     or TMDB and not cache[m["path"]].get("tried"))
            or cache[m["path"]].get("ov") != m.get("ov")]
    if not movies:
        sys.exit("No movies found - is the NAS mounted?")  # rather than overwriting the output with an empty catalog
    new_paths = {m["path"] for m in todo}
    print(f"[metadata] {len(movies)} entries, {len(todo)} to look up")
    for m in todo:
        year_label = f" ({m['year']})" if m.get("year") else ""
        print(f"[metadata] new/changed: {m['title']}{year_label}")
    with ThreadPoolExecutor(8) as ex:
        for i, r in enumerate(ex.map(lookup, todo), 1):
            # A "poster" override applies no matter which lookup path found the movie (by tmdb id,
            # imdb id, title search, or OMDb) - none of those look at it themselves, only "manual"
            # mode does, since it builds the whole row from the override alone.
            if (r.get("ov") or {}).get("poster"):
                r["poster"] = r["ov"]["poster"]
            cache[r["path"]] = r
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}")
    print("[metadata] lookup stage complete")
    # With -n the follow-up lookups below still run, but only for the movies just added - existing ones
    # aren't re-queried.
    in_scope = lambda c: not SCAN_ONLY or c.get("path") in new_paths
    old = [c for c in cache.values() if c.get("imdb_id") and "released" not in c and in_scope(c)]
    if old:
        print(f"adding release dates to {len(old)} cached movies")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(backfill_date, old))
    def score_still_pending(c):
        """True if a TMDB-sourced movie hasn't been scored yet, or was "scored" with no real IMDb
        rating (OMDb's imdbRating: N/A) for a recent release that may simply not have one *yet* -
        e.g. one that just released - and so is worth asking OMDb about again on a later run, rather
        than treated as a permanently settled "no score exists" the way an old movie's N/A would be."""
        if not c.get("scored"):
            return True
        if c.get("imdb") is not None:
            return False
        released = c.get("released")
        if not released:
            return False
        try:
            age_days = (datetime.now() - datetime.strptime(released, "%Y-%m-%d")).days
        except ValueError:
            return False
        return 0 <= age_days <= 180

    todo_scores = [c for c in cache.values() if c.get("src") == "tmdb" and c.get("imdb_id") and score_still_pending(c) and in_scope(c)]
    if todo_scores and OMDB:
        print(f"adding IMDb/RT scores to {len(todo_scores)} TMDB-sourced movies")
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(backfill_scores, todo_scores))
        if OMDB_DOWN:
            print("  OMDb daily limit reached; scores will be added on a later run")
    if TMDB:
        need = [c for c in cache.values() if c.get("imdb_id") and in_scope(c)
                and ("au" not in c or "tmdb" not in c or "tmdb_id" not in c or "country" not in c)]
        if need:
            print(f"adding Australian classification and TMDB score to {len(need)} movies")
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(backfill_au, need))
    new_found = [cache[p] for p in sorted(new_paths) if p in cache]
    if new_found:
        print(f"[metadata] {len(new_found)} new/changed movie(s) on disk:")
        for c in new_found:
            if c.get("found"):
                print(f"  + {c['title']} -> {_movie_line(c)}")
            else:
                print(f"  ? {c['title']} -> NO MATCH ({c.get('path')})")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    rows = [cache[m["path"]] for m in movies]
    rows.sort(key=lambda r: (r.get("name") or r["title"]).lower())
    # has_sub is only recomputed for the rows a partial run (-n/-s) checks, and it used to live nowhere
    # but the in-memory rows - so every other row would lose its SUB badge. Seed the rest from the
    # previous movies.json (the cache gets it from now on, see below).
    prev_json = HERE / "movies.json"
    if prev_json.exists():
        prev_sub = {m["path"]: sub_badge(m) for m in json.loads(prev_json.read_text()).get("movies", [])
                    if m.get("path")}
        for r in rows:
            if "sub" not in r and r.get("path") in prev_sub:
                r["sub"] = prev_sub[r["path"]]
                r["has_sub"] = r["sub"] is not None
    new_rows = [r for r in rows if r.get("path") in new_paths]
    if SCAN_ONLY:
        if new_rows:
            update_subtitles(new_rows)
        else:
            print("[subtitles] no new movies to check")
    else:
        update_subtitles(rows)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))  # rows are the cache's own dicts: persists has_sub
    if SCAN_ONLY:
        discover_cache = json.loads(DISCOVER_CACHE.read_text()) if DISCOVER_CACHE.exists() else {}
        lib_imdb_ids = {r.get("imdb_id") for r in rows if r.get("found") and r.get("imdb_id")}
        discover = {y: [m for m in ms if m.get("imdb_id") not in lib_imdb_ids]
                    for y, ms in discover_cache.items()}
        print(f"[discover] cached mode: reused {sum(map(len, discover.values()))} cached movies")
    else:
        print("[discover] finding highly rated movies not in the library")
        discover = build_discover(cache, rows)
    print(f"[discover] ready: {sum(map(len, discover.values()))} movies across {len(discover)} years")
    print("[output] writing movies.html and movies.json")
    write_html(rows, discover)
    write_json(rows, discover)
    misses = [r for r in rows if not r.get("found")]
    print(f"unmatched: {len(misses)}  -> {HERE/'movies.html'}")
    for m in misses:
        print(f"  {m['path']}")


def slim(m):
    """A movie row without the internal/heavy fields (_tmdb, ov) - for movies.json, read by the Android TV app."""
    return {k: v for k, v in m.items() if k not in ("_tmdb", "ov", "error", "tried", "scored", "src")}


def write_html(rows, discover):
    disc_grids = "".join(f'<div class="grid" data-y="{html.escape(y)}">{"".join(map(card, movies))}</div>'
                          for y, movies in discover.items())
    (HERE / "movies.html").write_text(PAGE.replace("__N__", str(len(rows)))
                                       .replace("__CARDS__", "".join(map(card, rows)))
                                       .replace("__DISCOVER_GRIDS__", disc_grids))


def publish_json(catalog):
    """Writes movies.json next to the script, and into each NAS root's movie-catalog/ folder (if
    present) so the app can read it over SMB, the same way it reads video files."""
    data = json.dumps(catalog, ensure_ascii=False)
    (HERE / "movies.json").write_text(data)
    for root in ROOTS:
        dest = root / "movie-catalog" / "movies.json"
        if dest.parent.is_dir():
            dest.write_text(data)
            print(f"  copied movies.json -> {dest}")


def write_json(rows, discover):
    """movies.json: same data as movies.html, for the Android TV app. Also dropped into each NAS root's
    movie-catalog/ folder (if present) so the app can read it over SMB, the same way it reads video files."""
    catalog = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "movies": [slim(r) for r in rows if r.get("found")],
        "discover": {y: [slim(m) for m in ms] for y, ms in discover.items()},
    }
    publish_json(catalog)


if __name__ == "__main__":
    main()
