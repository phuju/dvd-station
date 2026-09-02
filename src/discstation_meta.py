"""Optional video (movie / TV) metadata lookup via TMDb.

All functions degrade to no-ops when `tmdbsimple` is not installed or no API key
is configured (env DISCSTATION_TMDB_API_KEY, or ~/.local/share/discstation/tmdb.key
/ ~/.config/discstation/tmdb.key).
"""
import json
import os
import re
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    import tmdbsimple as _tmdb
except Exception:
    _tmdb = None

_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _api_key():
    key = os.environ.get("DISCSTATION_TMDB_API_KEY", "").strip()
    if key:
        return key
    for cand in (
        Path.home() / ".local/share/discstation/tmdb.key",
        Path.home() / ".config/discstation/tmdb.key",
    ):
        try:
            k = cand.read_text().strip()
            if k:
                return k
        except OSError:
            pass
    return None


def available():
    return _tmdb is not None and _api_key() is not None


def _clean_title(raw):
    """Turn a disc label / filename stem into a plausible search title + year."""
    if not raw:
        return "", None
    name = str(raw).replace("_", " ").strip()
    year = None
    m = _YEAR_RE.search(name)
    if m:
        year = m.group(0)
        name = (name[:m.start()] + " " + name[m.end():])
    name = re.sub(r"\b(disc\s*\d*|dvd|video[_ ]?ts|bluray|blu-ray|pal|ntsc|"
                  r"region\s*\d|season\s*\d+|s\d+|d\d+)\b", " ", name, flags=re.I)
    name = re.sub(r"[._()\[\]]+", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name, year


def lookup(title_guess, year=None):
    """Return {title, year, tmdb_id, media_type, poster_url, overview} or None."""
    if not available():
        return None
    name, guessed_year = _clean_title(title_guess)
    year = year or guessed_year
    if not name:
        return None
    _tmdb.API_KEY = _api_key()
    try:
        search = _tmdb.Search()
        movie_kwargs = {"query": name}
        if year:
            movie_kwargs["year"] = year
        results = (search.movie(**movie_kwargs).get("results") or [])
        media_type = "movie"
        if not results:
            tv_kwargs = {"query": name}
            if year:
                tv_kwargs["first_air_date_year"] = year
            results = (search.tv(**tv_kwargs).get("results") or [])
            media_type = "tv"
        if not results:
            return None
        top = results[0]
        released = top.get("release_date") or top.get("first_air_date") or ""
        return {
            "title": top.get("title") or top.get("name") or name,
            "year": released[:4] or (year or ""),
            "tmdb_id": top.get("id"),
            "media_type": media_type,
            "poster_url": _IMG_BASE + top["poster_path"] if top.get("poster_path") else None,
            "overview": top.get("overview") or "",
        }
    except Exception as e:  # tmdbsimple raises its own APIKeyError / HTTPError types
        print(f"TMDb lookup failed: {e}")
        return None


def folder_name(meta):
    base = re.sub(r'[\\/:*?"<>|]+', "_", (meta.get("title") or "").strip())
    base = re.sub(r"\s+", " ", base).strip()
    year = meta.get("year")
    if base and year:
        return f"{base} ({year})"
    return base or ""


def _nfo(meta):
    tag = "tvshow" if meta.get("media_type") == "tv" else "movie"

    def esc(value):
        return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<{tag}>",
        f"  <title>{esc(meta.get('title', ''))}</title>",
        f"  <year>{esc(meta.get('year', ''))}</year>",
        f"  <plot>{esc(meta.get('overview', ''))}</plot>",
        f'  <uniqueid type="tmdb" default="true">{esc(meta.get("tmdb_id", ""))}</uniqueid>',
        f"</{tag}>",
        "",
    ])


def save_assets(out_dir, meta):
    """Write movie.nfo and poster.jpg into out_dir (best effort)."""
    out_dir = Path(out_dir)
    try:
        (out_dir / "movie.nfo").write_text(_nfo(meta), encoding="utf-8")
    except OSError:
        pass
    if meta.get("poster_url") and requests is not None:
        try:
            r = requests.get(meta["poster_url"], timeout=30)
            if r.status_code == 200 and r.content:
                (out_dir / "poster.jpg").write_bytes(r.content)
        except Exception:
            pass


if __name__ == "__main__":  # quick manual test: python3 discstation_meta.py "The Matrix 1999"
    import sys
    print("available:", available())
    if len(sys.argv) > 1:
        print(json.dumps(lookup(" ".join(sys.argv[1:])), indent=2))
