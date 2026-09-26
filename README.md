# movie-catalog

Scans a movies folder (for example one mounted from a NAS) and builds a single, searchable `movies.html` page. Each movie gets a poster, a short summary, genres, runtime, release date, IMDb and Rotten Tomatoes scores, the TMDB user score and the Australian classification.

No server and no dependencies: one Python script that uses only the standard library, and one static HTML file as the output.

## Setup

1. Python 3.8 or newer.
2. Get two free API keys:
   - **OMDb**: <https://www.omdbapi.com/apikey.aspx> (IMDb and Rotten Tomatoes scores, posters, summaries)
   - **TMDB**: <https://www.themoviedb.org/settings/api> (fallback matching, Chinese titles, TMDB score, Australian classification)
3. Put the keys in a `.env` file next to `build.py`:

   ```
   OMDB_API_KEY=your_omdb_key
   TMDB_API_KEY=your_tmdb_key
   ```

   `.env` is git-ignored. Either a TMDB v3 API key or a v4 read-access token works.

## Usage

```bash
python3 build.py                 # scans /Volumes/movies and /Volumes/movies-2T
python3 build.py /path/to/movies # or scan your own folder(s)
open movies.html
```

Run it again whenever you add movies or edit `overrides.json`. Lookups are cached in `cache.json`, so a re-run only fetches what's new or changed.

The OMDb free tier allows 1,000 requests a day. If the limit is hit, the script carries on with TMDB alone and adds the IMDb and Rotten Tomatoes scores on a later run.

## Folder layout it expects

```
movies/
  2023/
    Oppenheimer (2023) [1080p]/
    Barbie.2023.1080p.WEB-DL.mkv
  cartoon/
    2019/
      Frozen II (2019) [1080p]
```

- Each top-level folder inside a root (a year, `cartoon`, `old`, …) holds movies, either as folders or as video files.
- A year subfolder inside a top-level folder, like `cartoon/2019/`, is looked into as well.
- These are skipped: `TV`, `tmp`, `TVseries`, `upload`, `4k`, and any file named like a TV episode (`S01E03`).
- Titles and years are read from messy release names (`Alien.Romulus.2024.1080p.AMZN.WEB-DL…`), including Chinese filenames like `2017水形物语HD1080P中字.mp4`.

To change the skipped folders, edit `SKIP_DIRS` at the top of `build.py`.

## The page

- Search, and a year filter (defaults to the current year, or All years if there are none).
- Sort by IMDb score (default), name, release date, Rotten Tomatoes or TMDB score.
- Click a poster to open that movie's folder in a new browser tab (a `file://` link, so it works best in Chrome-based browsers).

## Fixing wrong or missing matches: `overrides.json`

Create `overrides.json` next to `build.py`. Each key is the movie's path relative to `/Volumes` (or to the parent of your root folder), exactly as shown at the bottom of its card:

```json
{
  "movies/2018/Some Odd Filename": { "imdb": "tt0257044" },
  "movies/2019/Another File.mkv":   { "tmdb": 4995 },
  "movies/2020/Bad Parse (2020":    { "title": "The Real Title", "year": 2020 },
  "movies/2021/Not A Movie":        { "hide": true }
}
```

| Entry | Effect |
|---|---|
| `{"imdb": "tt…"}` / `{"tmdb": 123}` | Pin the exact movie. The most reliable fix. |
| `{"title": "…", "year": 2020}` | Change what is searched for. |
| `{"hide": true}` | Leave the entry out of the page. |

After editing, re-run `build.py`. Only the changed entries are looked up again.

## Files

| File | Purpose |
|---|---|
| `build.py` | The scanner and page generator |
| `.env` | Your API keys (git-ignored) |
| `overrides.json` | Your manual fixes (git-ignored) |
| `cache.json` | Lookup results (git-ignored, safe to delete; the next run rebuilds it) |
| `movies.html` | The generated page (git-ignored) |

## Data sources

Movie data comes from [OMDb](https://www.omdbapi.com/) and [TMDB](https://www.themoviedb.org/). This product uses the TMDB API but is not endorsed or certified by TMDB.
