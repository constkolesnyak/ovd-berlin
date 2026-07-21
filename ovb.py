#!/usr/bin/env python3
"""Find original-language screenings in Berlin cinemas from ov-berlin.info.

The site's RSS feeds carry no language or subtitle information, so this reads
the movie index page (language + version live in data-* attributes) and, for
the movies that survive filtering, the JSON-LD ScreeningEvent blocks on each
movie page for exact showtimes, cinemas and ticket links.

Examples:
    ./ovb.py --lang japanese                 # Japanese audio, English subs (default)
    ./ovb.py --lang korean --subs any        # any subtitle version
    ./ovb.py --days 3 --min-imdb 7.5
    ./ovb.py --lang japanese --group movie --links
    ./ovb.py --list langs                    # what languages are on right now
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://ov-berlin.info"
INDEX_URL = f"{BASE}/movies/"
CINEMAS_URL = f"{BASE}/cinemas/"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
UA = "ovb.py/1.0 (+personal screening lookup)"
DEFAULT_TTL = 6 * 3600

# Subtitle versions used by the site: OV (no subs), OmU (German subs),
# OmeU (English subs). They can be prefixed (3D OmU, 70mm OV, IMAX OmU).
SUBS_ALIASES = {
    "omeu": "omeu", "en": "omeu", "eng": "omeu", "english": "omeu",
    "omu": "omu", "de": "omu", "ger": "omu", "german": "omu",
    "ov": "ov", "none": "ov",
    "any": "any", "all": "any",
}


# --------------------------------------------------------------------------- io


def fetch(url: str, ttl: int, refresh: bool = False) -> str:
    """GET url, caching the body on disk for ttl seconds."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_text(encoding="utf-8")

    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
            path.write_text(body, encoding="utf-8")
            return body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(1 + attempt)
    if path.exists():  # stale cache beats no data
        print(f"warning: {url} failed ({last}), using stale cache", file=sys.stderr)
        return path.read_text(encoding="utf-8")
    raise SystemExit(f"error: could not fetch {url}: {last}")


# ---------------------------------------------------------------------- parsing

DAY_RE = re.compile(
    r'data-day-id="([^"]*)"[^>]*>.*?data-cinema-count="([^"]*)"[^>]*>.*?data-versions="([^"]*)"',
    re.S,
)
GENRE_RE = re.compile(r'/icons/tags\.svg"[^>]*>([^<]*)<')
SLUG_RE = re.compile(r'href="(/movies/[^"]+)"')
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def parse_index(page: str) -> list[dict]:
    """Pull one record per movie out of the /movies/ index page."""
    movies = []
    for chunk in page.split('<table class="table w-100 movie-table"')[1:]:
        head = chunk[: chunk.find(">")]
        body = chunk[: chunk.find("</table>")]

        def attr(name: str, default: str = "") -> str:
            m = re.search(rf'{name}="([^"]*)"', head)
            return html.unescape(m.group(1)) if m else default

        def attr_json(name: str, default):
            raw = attr(name)
            try:
                return json.loads(raw) if raw else default
            except json.JSONDecodeError:
                return default

        # per-day cinema count and available versions, plus an "all-days" summary
        days = {
            day: {"cinemas": int(count or 0), "versions": versions}
            for day, count, versions in DAY_RE.findall(body)
        }
        slug = SLUG_RE.search(body)
        genres = GENRE_RE.search(body)

        movies.append(
            {
                "id": attr("data-movie-id"),
                "title": attr("data-movie-title"),
                "year": attr("data-movie-year"),
                "imdb": attr("data-imdb-score"),
                "lb": attr("data-letterboxd-score"),
                "rt": attr("data-rt-tomatometer"),
                "languages": attr_json("data-languages", []),
                "genres": [g.strip() for g in genres.group(1).split(",")] if genres else [],
                "days": {d: v for d, v in days.items() if d != "all-days"},
                "versions": days.get("all-days", {}).get("versions", ""),
                "url": BASE + slug.group(1) if slug else "",
            }
        )
    return movies


def parse_screenings(page: str) -> tuple[dict, list[dict]]:
    """Extract movie metadata and ScreeningEvents from a movie page's JSON-LD."""
    meta: dict = {}
    events: list[dict] = []
    for block in LD_RE.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Movie":
                continue
            duration = node.get("duration") or ""
            minutes = re.search(r"PT(\d+)M", duration)
            meta = {
                "genres": node.get("genre") or [],
                "runtime": int(minutes.group(1)) if minutes else None,
                "directors": [d.get("name") for d in node.get("director") or []],
            }
            for ev in node.get("subjectOf") or []:
                if ev.get("@type") != "ScreeningEvent":
                    continue
                loc = ev.get("location") or {}
                events.append(
                    {
                        "start": ev.get("startDate", ""),
                        "cinema": loc.get("name", "?"),
                        "district": (loc.get("address") or {}).get("addressRegion", ""),
                        "version": ev.get("subtitleLanguage", ""),
                        "tickets": ev.get("url", ""),
                    }
                )
    return meta, events


def parse_cinemas(page: str) -> list[str]:
    """Cinema names from the ItemList JSON-LD on /cinemas/."""
    names = []
    for block in LD_RE.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "ItemList":
                continue
            for entry in node.get("itemListElement") or []:
                name = (entry.get("item") or {}).get("name")
                if name:
                    names.append(name)
    return names


# -------------------------------------------------------------------- filtering


def subs_kind(version: str) -> str:
    """Classify a version string ('3D OmU', '70mm OV, IMAX OmU', ...)."""
    v = version.lower()
    if "omeu" in v:
        return "omeu"
    if "omu" in v:
        return "omu"
    return "ov"


def version_matches(version: str, want: str) -> bool:
    """A movie-level version string may list several versions at once."""
    if want == "any":
        return True
    return any(subs_kind(part) == want for part in version.split(","))


def matches_terms(values, terms) -> bool:
    """Case-insensitive substring match of any term against any value."""
    if not terms:
        return True
    low = [str(v).lower() for v in values]
    return any(t in v for t in terms for v in low)


def parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"error: bad date {text!r}, expected YYYY-MM-DD")


# ---------------------------------------------------------------------- output

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def style(text: str, code: str, on: bool) -> str:
    return f"{code}{text}{RESET}" if on else text


def ratings(m: dict) -> str:
    bits = []
    if m["imdb"]:
        bits.append(f"imdb {m['imdb']}")
    if m["lb"]:
        bits.append(f"lb {m['lb']}")
    if m["rt"]:
        bits.append(f"rt {m['rt']}%")
    return "  ".join(bits)


def print_by_day(rows: list[dict], args, color: bool) -> None:
    current = None
    for row in sorted(rows, key=lambda r: (r["start"], r["title"])):
        day = row["start"][:10]
        if day != current:
            current = day
            label = dt.date.fromisoformat(day).strftime("%a %d %b")
            print("\n" + style(label, BOLD, color))
        title = f"{row['title']} ({row['year']})" if row["year"] else row["title"]
        place = f"{row['cinema']}" + (f", {row['district']}" if row["district"] else "")
        version = "" if args.subs != "any" else f"  [{row['version']}]"
        print(
            f"  {row['start'][11:16]}  {title}{version}\n"
            f"         {style(place, DIM, color)}"
            + (f"  {style(ratings(row), DIM, color)}" if ratings(row) else "")
        )
        if args.links and row["tickets"]:
            print(f"         {style(row['tickets'], DIM, color)}")


def print_by_movie(rows: list[dict], args, color: bool) -> None:
    movies: dict[str, list[dict]] = {}
    for row in rows:
        movies.setdefault(row["id"], []).append(row)

    def sort_key(shows):
        if args.sort == "rating":
            return (-float(shows[0]["imdb"] or 0), shows[0]["title"])
        if args.sort == "title":
            return (shows[0]["title"],)
        return (min(s["start"] for s in shows), shows[0]["title"])

    for shows in sorted(movies.values(), key=sort_key):
        m = shows[0]
        head = f"{m['title']} ({m['year']})" if m["year"] else m["title"]
        facts = [", ".join(m["genres"][:3])] if m["genres"] else []
        if m.get("runtime"):
            facts.append(f"{m['runtime']} min")
        if m.get("directors"):
            facts.append(m["directors"][0])
        if ratings(m):
            facts.append(ratings(m))
        print("\n" + style(head, BOLD, color) + (
            "  " + style("· ".join(f"{f} " for f in facts), DIM, color) if facts else ""
        ))
        for s in sorted(shows, key=lambda r: r["start"]):
            when = dt.datetime.fromisoformat(s["start"]).strftime("%a %d %b %H:%M")
            place = s["cinema"] + (f", {s['district']}" if s["district"] else "")
            version = "" if args.subs != "any" else f"  [{s['version']}]"
            print(f"  {when}  {place}{version}")
            if args.links and s["tickets"]:
                print(f"                     {style(s['tickets'], DIM, color)}")
        print("  " + style(m["url"], DIM, color))


# ------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Find original-language screenings in Berlin (ov-berlin.info).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1],
    )
    p.add_argument("--lang", "-l", action="append", default=[], metavar="NAME",
                   help="spoken language, repeatable or comma-separated (e.g. japanese)")
    p.add_argument("--not-lang", action="append", default=[], metavar="NAME",
                   help="drop movies listing this language")
    p.add_argument("--subs", "-s", default="omeu", metavar="KIND",
                   help="omeu (English subs, default) | omu (German) | ov (none) | any")
    p.add_argument("--days", "-d", type=int, metavar="N",
                   help="window length in days from --from; default is everything "
                        "the site publishes (~15 days out)")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="default today")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="overrides --days")
    p.add_argument("--genre", "-g", action="append", default=[], metavar="NAME")
    p.add_argument("--cinema", "-c", action="append", default=[], metavar="NAME")
    p.add_argument("--title", "-t", action="append", default=[], metavar="TEXT")
    p.add_argument("--min-imdb", type=float, metavar="X")
    p.add_argument("--min-lb", type=float, metavar="X", help="minimum Letterboxd score")
    p.add_argument("--group", choices=["day", "movie"], default="day")
    p.add_argument("--sort", choices=["date", "rating", "title"], default="date",
                   help="order for --group movie (default date)")
    p.add_argument("--links", action="store_true", help="show ticket links")
    p.add_argument("--json", action="store_true", help="dump screenings as JSON")
    p.add_argument("--list", choices=["langs", "cinemas", "genres"],
                   help="list available values instead of screenings")
    p.add_argument("--refresh", "-r", action="store_true", help="bypass the cache")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL, metavar="SEC",
                   help=f"cache lifetime in seconds (default {DEFAULT_TTL})")
    p.add_argument("--no-color", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()

    want_subs = SUBS_ALIASES.get(args.subs.lower())
    if want_subs is None:
        raise SystemExit(f"error: unknown --subs {args.subs!r}; try omeu, omu, ov, any")
    args.subs = want_subs

    def split_terms(values):
        return [t.strip().lower() for v in values for t in v.split(",") if t.strip()]

    langs = split_terms(args.lang)
    not_langs = split_terms(args.not_lang)
    genres = split_terms(args.genre)
    cinemas = split_terms(args.cinema)
    titles = split_terms(args.title)

    # No explicit window means "as far as the site goes" — it publishes about
    # 15 days, and the last week of that is only partly filled in, because
    # Berlin cinemas announce their programme one Thursday-to-Wednesday week
    # at a time.
    start = parse_date(args.date_from) if args.date_from else dt.date.today()
    if args.date_to:
        end = parse_date(args.date_to)
    elif args.days is not None:
        end = start + dt.timedelta(days=args.days - 1)
    else:
        end = dt.date.max
    if end < start:
        raise SystemExit("error: --to is before --from")

    color = sys.stdout.isatty() and not args.no_color

    if args.list == "cinemas":
        for name in parse_cinemas(fetch(CINEMAS_URL, args.ttl, args.refresh)):
            print(name)
        return 0

    movies = parse_index(fetch(INDEX_URL, args.ttl, args.refresh))
    if not movies:
        raise SystemExit("error: parsed 0 movies — the page layout probably changed")

    if args.list:
        key = {"langs": "languages", "genres": "genres"}[args.list]
        counts: dict[str, int] = {}
        for m in movies:
            for value in m[key]:
                counts[value] = counts.get(value, 0) + 1
        for value, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{n:4}  {value}")
        return 0

    # Stage 1: cheap filtering on index data, so we only fetch pages we need.
    candidates = [
        m for m in movies
        if matches_terms(m["languages"], langs)
        and not (not_langs and matches_terms(m["languages"], not_langs))
        and matches_terms(m["genres"], genres)
        and matches_terms([m["title"]], titles)
        and version_matches(m["versions"], args.subs)
        and (args.min_imdb is None or float(m["imdb"] or 0) >= args.min_imdb)
        and (args.min_lb is None or float(m["lb"] or 0) >= args.min_lb)
        and any(start <= parse_date(d) <= end for d in m["days"] if d[:1].isdigit())
    ]
    if not candidates:
        print("No movies match those filters.", file=sys.stderr)
        return 1

    # Stage 2: exact showtimes from each movie page.
    def load(m: dict) -> dict:
        meta, events = parse_screenings(fetch(m["url"], args.ttl, args.refresh))
        return {**m, **meta, "screenings": events}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        detailed = list(pool.map(load, candidates))

    rows = []
    for m in detailed:
        for s in m["screenings"]:
            day = s["start"][:10]
            if not day or not (start <= parse_date(day) <= end):
                continue
            if args.subs != "any" and subs_kind(s["version"]) != args.subs:
                continue
            if not matches_terms([s["cinema"], s["district"]], cinemas):
                continue
            rows.append({**{k: v for k, v in m.items() if k != "screenings"}, **s})

    if not rows:
        print("No screenings match those filters.", file=sys.stderr)
        return 1

    if args.json:
        json.dump(sorted(rows, key=lambda r: r["start"]), sys.stdout,
                  ensure_ascii=False, indent=2)
        print()
        return 0

    labels = {"omeu": "English subtitles", "omu": "German subtitles",
              "ov": "no subtitles", "any": "any version"}
    scope = f"{'/'.join(l.title() for l in langs)} · " if langs else ""
    n_movies = len({r["id"] for r in rows})
    plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    first = parse_date(min(r["start"][:10] for r in rows))
    last = parse_date(max(r["start"][:10] for r in rows))
    span = f"{first:%d %b}" if first == last else f"{first:%d %b} – {last:%d %b %Y}"
    print(f"{scope}{labels[args.subs]} · {span} · "
          f"{plural(n_movies, 'movie')}, {plural(len(rows), 'screening')}")

    (print_by_movie if args.group == "movie" else print_by_day)(rows, args, color)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # `./ovb.py | head` closes the pipe early; also silence the final flush
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
