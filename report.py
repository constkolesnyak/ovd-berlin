#!/usr/bin/env python3
"""Japanese-language screenings in Berlin as a Telegram post: poster collage + one message.

Reuses the scraping, caching and parsing already in ovb.py, then adds the three
things a chat post needs: posters composited into one image, a listing squeezed
under Telegram's 4096-character ceiling, and delivery through the Bot API.

    ./report.py                 # dry run: print the message, write collage.png
    ./report.py --send          # post it to TELEGRAM_CHAT_ID
    ./report.py --from 2026-08-04 --lang Korean

Credentials come from the environment or a .env file next to this script:
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Never hardcode them here.
"""

from __future__ import annotations

import argparse
import colorsys
import concurrent.futures
import datetime as dt
import html
import io
import json
import math
import mimetypes
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont,
                   ImageOps)

import ovb

HERE = Path(__file__).resolve().parent
POSTER_DIR = ovb.CACHE_DIR / "posters"
EVENTS_URL = f"{ovb.BASE}/events"
BLACKLIST_FILE = HERE / "cinema-blacklist.txt"

# Telegram counts UTF-16 code units, not characters: an emoji costs 2.
TEXT_LIMIT = 4096
PHOTO_BYTES_LIMIT = 10 * 1024 * 1024

# Collage geometry. Telegram re-encodes anything larger than ~2560px on its
# long side, so we stay just under that and hand it a lossless PNG: the fewer
# resamples between here and the phone screen, the sharper the posters.
MAX_LONG_SIDE = 2560
# A wide outer frame, not just breathing room: on a phone the notch bites into
# the top of a tall image, and this is what it lands on instead of a poster.
# The backdrop fills it, so it reads as part of the picture.
MARGIN = 84
GUTTER = 14
BACKGROUND = (15, 17, 21)
PLACEHOLDER = (38, 41, 48)
NUMBER_INK = (248, 244, 238, 255)  # warm off-white; #fff reads as pasted-on UI
# The backdrop: each poster's palette, spread into soft fields and screened
# onto near-black. SPREAD is how far past its cell a poster's colours reach,
# BLUR how much they melt together, GLOW the brightness — past ~1.1 it starts
# competing with the posters themselves.
BACKDROP_SPREAD = 1.5
BACKDROP_BLUR = 7
BACKDROP_GLOW = 0.95
# What every sheet aims at. It is only a target: the grid has to be whole rows
# of whole posters, so six of them land at 0.49 rather than here. The notch
# that used to make such a tall sheet a problem is handled by MARGIN instead.
#
# This is deliberately chosen over pleasing Telegram's mosaic. Its grouped-media
# layout sorts images into wide (>1.2), narrow (<0.8) and square and crops each
# to fill its tile, and portrait sheets land in the narrow class, which gets the
# lopsided arrangements. Squaring the sheets off would tile better but is not
# what a phone screen wants, and tapping a tile always shows the sheet whole.
TARGET_ASPECT = 0.70


# ------------------------------------------------------------------------ setup


def load_env(path: Path = HERE / ".env") -> None:
    """Populate os.environ from a .env file without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_blacklist(path: Path = BLACKLIST_FILE) -> set[str]:
    """Venue names to leave out, one per line, # for comments."""
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.add(name.casefold())
    return names


# ---------------------------------------------------------------------- scraping


def movie_details(page: str) -> dict:
    """Everything the JSON-LD Movie node knows, including its ScreeningEvents.

    ovb.parse_screenings() returns only what its terminal output needs; a chat
    post wants the poster, the original title and the outbound links too.
    """
    for block in ovb.LD_RE.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Movie":
                continue

            def names(field):
                out = []
                for item in node.get(field) or []:
                    out.append(item.get("name") if isinstance(item, dict) else item)
                return [n for n in out if n]

            minutes = re.search(r"PT(\d+)M", node.get("duration") or "")
            links = {}
            for url in node.get("sameAs") or []:
                for host in ("imdb", "letterboxd", "rottentomatoes"):
                    if host in url:
                        links[host] = url

            screenings = []
            for ev in node.get("subjectOf") or []:
                if ev.get("@type") != "ScreeningEvent":
                    continue
                loc = ev.get("location") or {}
                screenings.append(
                    {
                        "start": ev.get("startDate", ""),
                        "cinema": loc.get("name", "?"),
                        "district": (loc.get("address") or {}).get("addressRegion", ""),
                        "version": ev.get("subtitleLanguage", ""),
                        "tickets": ev.get("url", ""),
                    }
                )

            return {
                "original_title": node.get("alternateName") or "",
                "description": node.get("description") or "",
                "runtime": int(minutes.group(1)) if minutes else None,
                "genres": node.get("genre") or [],
                "directors": names("director"),
                "cast": names("actor"),
                "countries": names("countryOfOrigin"),
                "content_rating": node.get("contentRating") or "",
                "links": links,
                "trailer": (node.get("trailer") or {}).get("embedUrl", ""),
                "poster": node.get("image") or "",
                "screenings": screenings,
            }
    return {"screenings": []}


def parse_events(page: str) -> dict:
    """Film festivals & events. The section is empty site-wide as of July 2026,
    so the populated markup is unverified — we report the count we can read and
    only claim individual events when we actually recognise cards."""
    count = None
    m = re.search(r"•\s*(\d+)\s*film events?", page)
    if m:
        count = int(m.group(1))
    empty = "No upcoming film festivals or events" in page

    titles = []
    for href, inner in re.findall(r'href="(/events/[^"]+)"[^>]*>(.*?)</a>', page, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", " ", inner)).strip()
        text = re.sub(r"\s+", " ", text)
        if text and (ovb.BASE + href, text) not in titles:
            titles.append((ovb.BASE + href, text))

    return {"count": 0 if empty else count, "items": titles}


# A film "in language X" is one where X leads the language list, or where the
# country of origin says so. Both rules are needed: Marty Supreme and Wings of
# Desire merely *contain* Japanese dialogue, while The Wind Rises is Ghibli
# through and through yet lists Japanese last (its TMDB record is odd).
HOMELANDS = {
    "Japanese": {"Japan"},
    "Korean": {"South Korea", "Korea"},
    "Chinese": {"China", "Taiwan", "Hong Kong"},
}


def is_native(movie: dict, lang: str) -> bool:
    langs = movie.get("languages") or []
    if lang not in langs:
        return False
    if langs[0] == lang:
        return True
    return bool(HOMELANDS.get(lang, set()) & set(movie.get("countries") or []))


def collect(args) -> tuple[list[dict], dt.date, dt.date]:
    """Movies in the requested language with at least one screening in range."""
    start = (
        dt.date.fromisoformat(args.date_from)
        if args.date_from
        else dt.date.today() + dt.timedelta(days=1)
    )
    end = dt.date.fromisoformat(args.date_to) if args.date_to else dt.date.max

    index = ovb.parse_index(ovb.fetch(ovb.INDEX_URL, args.ttl, args.refresh))
    if not index:
        raise SystemExit("error: parsed 0 movies — the index layout probably changed")
    candidates = [m for m in index if args.lang in m["languages"]]

    def load(m: dict) -> dict:
        return {**m, **movie_details(ovb.fetch(m["url"], args.ttl, args.refresh))}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        detailed = list(pool.map(load, candidates))

    blocked = set() if args.no_blacklist else load_blacklist()
    movies, foreign, skipped_venues = [], [], {}
    for m in detailed:
        shows = [
            s for s in m["screenings"]
            if s["start"][:10] and start <= dt.date.fromisoformat(s["start"][:10]) <= end
        ]
        if blocked:
            keep = []
            for s in shows:
                if s["cinema"].casefold() in blocked:
                    skipped_venues[s["cinema"]] = skipped_venues.get(s["cinema"], 0) + 1
                else:
                    keep.append(s)
            shows = keep
        if not shows:
            continue
        if not args.loose and not is_native(m, args.lang):
            foreign.append(f'{m["title"]} ({", ".join(m.get("countries") or ["?"])})')
            continue
        movies.append({**m, "screenings": sorted(shows, key=lambda s: s["start"])})

    if skipped_venues:
        detail = ", ".join(f"{v} ({n})" for v, n in sorted(skipped_venues.items()))
        print(f"note: blacklist dropped {sum(skipped_venues.values())} screenings — {detail}",
              file=sys.stderr)
    for name in sorted(blocked - {v.casefold() for v in skipped_venues}):
        print(f"note: blacklist entry {name!r} matched nothing", file=sys.stderr)
    if foreign:
        print(f"note: skipped {len(foreign)} film(s) that merely feature {args.lang} — "
              + "; ".join(foreign) + " (use --loose to keep them)", file=sys.stderr)

    movies.sort(key=lambda m: (m["screenings"][0]["start"], m["title"]))
    if not movies:
        return [], start, end
    last = max(dt.date.fromisoformat(s["start"][:10]) for m in movies for s in m["screenings"])
    return movies, start, min(end, last)


# ----------------------------------------------------------------------- posters


def fetch_poster(url: str, ttl: int, refresh: bool = False) -> bytes | None:
    if not url:
        return None
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTER_DIR / url.rsplit("/", 1)[-1]
    if not refresh and path.exists() and dt.datetime.now().timestamp() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ovb.UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        path.write_bytes(body)
        return body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"warning: poster {url} failed ({exc})", file=sys.stderr)
        return path.read_bytes() if path.exists() else None


def choose_columns(n: int, cell_ratio: float, target: float = TARGET_ASPECT) -> int:
    """Pick a column count that leaves few holes and lands near `target` aspect.

    A cell of width 1 is 1/cell_ratio tall, so a cols x rows grid has aspect
    cols * cell_ratio / rows.
    """
    # Portrait is a requirement, not a preference, so landscape grids are only
    # considered if nothing taller exists. Six posters sit exactly between 3x2
    # and 2x3 — both a factor of 1.5 off the target — and which way that tie
    # falls would otherwise depend on the median poster being a hair over or
    # under 0.70, which is no way to decide the shape of the post.
    for portrait_only in (True, False):
        best, best_score = None, math.inf
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            aspect = cols * cell_ratio / rows
            if portrait_only and aspect > 1.0:
                continue
            holes = cols * rows - n
            # Holes are penalised hard: a full grid beats a better-proportioned
            # one with gaps, which is what keeps an explicit --chunk 9 at 3x3.
            score = abs(math.log(aspect / target)) + holes * 0.25
            if score < best_score:
                best, best_score = cols, score
        if best is not None:
            return best
    return 1


def split_packed(items: list, size: int) -> list[list]:
    """Full sheets of `size`, whatever is left over centred on the last one."""
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def row_plan(n: int, cols: int, rows: int) -> list[int]:
    """How many posters go in each row of a sheet holding n of them.

    Not "fill each row, remainder last": a short sheet spreads out instead, so
    3 posters read as 2 over 1 and 4 as 2 over 2 rather than a full row with a
    stub under it. Rows are as even as possible, wider ones on top, and a full
    sheet still comes out as plain full rows.
    """
    used = min(rows, max(1, math.ceil(n / max(1, cols - 1))))
    base, extra = divmod(n, used)
    return [base + (1 if i < extra else 0) for i in range(used)]


def number_font(size: int) -> ImageFont.FreeTypeFont:
    """A rounded grotesque if the system has one — digits in a disc want it."""
    for path in ("/System/Library/Fonts/SFNSRounded.ttf",
                 "/System/Library/Fonts/SFCompactRounded.ttf",
                 "/System/Library/Fonts/Avenir Next.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)  # ugly but never missing


def draw_numbers(canvas: Image.Image, boxes: list[tuple[int, int]], start: int,
                 cell_w: int, plan: list[int]) -> None:
    """Number each poster to match the listing, out in the margin beside it.

    The disc sits in the frame rather than on the artwork — stuck over a
    poster's corner it reads as a sticker. Rows of two put their badges on the
    outer side of each poster, mirrored; a wider row has no outer margin to
    use, so those fall back to a numeral inset in the top-left corner.

    The ink is warm off-white rather than pure white: against poster artwork
    #fff reads as a UI element pasted on top, and this sits with the paper.
    """
    pen = ImageDraw.Draw(canvas, "RGBA")
    radius = max(14, int(MARGIN * 0.40))
    font = number_font(int(radius * 1.25))

    n = start
    seen = 0
    for in_row in plan:
        for j in range(in_row):
            x0, y0 = boxes[seen + j]
            if in_row <= 2:
                cx = x0 - MARGIN / 2 if j == 0 else x0 + cell_w + MARGIN / 2
                cy = y0 + radius
                pen.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                            fill=(10, 11, 15, 225), outline=(255, 255, 255, 70), width=2)
                pen.text((cx, cy - radius * 0.05), str(n), font=font,
                         fill=NUMBER_INK, anchor="mm")
            else:
                pen.text((x0 + radius, y0 + radius), str(n), font=font,
                         fill=NUMBER_INK, anchor="mm",
                         stroke_width=max(2, radius // 10), stroke_fill=(0, 0, 0, 170))
            n += 1
        seen += in_row


def palette_patch(im: Image.Image, cols: int = 4, rows: int = 6,
                  sub: int = 4) -> Image.Image:
    """A tiny map of the poster's colours — all of them, where they actually are.

    Averaging a region gives grey, because a poster is mostly paper and print.
    So each block keeps its most vivid pixel instead: the sky stays blue, the
    title red, the shadows their own colour, and nothing desaturated survives
    to smear the backdrop into mud when this gets blurred.
    """
    small = im.convert("RGB").resize((cols * sub, rows * sub), Image.LANCZOS)
    pixels = small.load()
    patch = Image.new("RGB", (cols, rows))
    for r in range(rows):
        for c in range(cols):
            best, chosen = -1.0, (0, 0, 0)
            for dy in range(sub):
                for dx in range(sub):
                    rgb = pixels[c * sub + dx, r * sub + dy]
                    _, sat, val = colorsys.rgb_to_hsv(*[x / 255 for x in rgb])
                    if sat * val > best:
                        best, chosen = sat * val, rgb
            hue, sat, val = colorsys.rgb_to_hsv(*[x / 255 for x in chosen])
            patch.putpixel((c, r), tuple(int(255 * x) for x in colorsys.hsv_to_rgb(
                hue, min(1.0, sat * 1.3), min(1.0, val * 1.05))))
    return patch


def backdrop(images: list, size: tuple[int, int], boxes: list[tuple[int, int]],
             cell_w: int, cell_h: int) -> Image.Image:
    """Colour bled out of each poster, from where that poster actually sits.

    The glow radiates from the posters themselves: every tile is placed at its
    real box, spread a little wider than the cell, and blurred, so what fills
    the empty part of a short sheet is only what leaks in from its neighbours.

    Cycling posters over the whole grid instead was the earlier mistake — the
    unused cells got tiles of their own, and a white poster landing on one side
    with a dark one on the other split the gap into visible light and dark
    halves. With nothing painted there, the falloff is symmetric by
    construction, because the rows above it are centred.
    """
    pool = [im for im in images if im]
    if not pool:
        return Image.new("RGB", size, BACKGROUND)

    # Each poster's whole palette, spread out from where that poster sits.
    # Blurring the artwork itself was what made this muddy — paper and print
    # average to grey-brown — so what gets blurred is palette_patch(), which
    # keeps every colour but drops everything desaturated.
    scale = 20
    layer = Image.new("RGB", (max(1, size[0] // scale), max(1, size[1] // scale)), (0, 0, 0))
    tile_w = max(2, int(cell_w / scale * BACKDROP_SPREAD))
    tile_h = max(2, int(cell_h / scale * BACKDROP_SPREAD))
    for (x0, y0), im in zip(boxes, images):
        if im is None:
            continue
        patch = palette_patch(im).resize((tile_w, tile_h), Image.BICUBIC)
        layer.paste(patch, (int((x0 + cell_w / 2) / scale - tile_w / 2),
                            int((y0 + cell_h / 2) / scale - tile_h / 2)))

    layer = layer.filter(ImageFilter.GaussianBlur(BACKDROP_BLUR)).resize(size, Image.LANCZOS)
    layer = ImageEnhance.Brightness(layer).enhance(BACKDROP_GLOW)

    # Screen onto near-black rather than blending: blending averages the blobs
    # back towards grey, screening accumulates them, so where two glows overlap
    # the colour gets brighter instead of duller.
    return ImageChops.screen(Image.new("RGB", size, BACKGROUND), layer)


def decode(posters: list[bytes | None]) -> list:
    images = []
    for blob in posters:
        if not blob:
            images.append(None)
            continue
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            images.append(im.convert("RGB"))
        except Exception as exc:  # a broken poster must not sink the post
            print(f"warning: undecodable poster ({exc})", file=sys.stderr)
            images.append(None)
    return images


def build_collages(posters: list[bytes | None], base: Path, chunk: int | None,
                   album: bool = True) -> list[tuple[Path, int, int]]:
    """One collage, or an album of equally-shaped ones.

    Splitting is what buys resolution: a single sheet of everything hits the
    2560px ceiling and each poster ends up around 283px, while chunks of a
    dozen or fewer let every cell render at its native 500px.

    Geometry is decided once for the whole album — same grid, same cell, same
    canvas — because Telegram tiles a media group by aspect ratio, and mixing
    shapes turns the collapsed preview into a ragged mosaic.
    """
    images = decode(posters)
    ratios = [im.width / im.height for im in images if im]
    cell_ratio = statistics.median(ratios) if ratios else 0.7
    widths = [im.width for im in images if im]

    if chunk:
        groups = split_packed(images, chunk)
        # Grid from the fullest sheet, not from the chunk: with fewer films
        # than a chunk there is only one sheet and it should not be mostly gaps.
        per = min(chunk, len(images))
        cols = choose_columns(per, cell_ratio, TARGET_ASPECT)
        rows = math.ceil(per / cols)
    else:
        groups = [images]
        cols = choose_columns(len(images), cell_ratio, TARGET_ASPECT)
        rows = math.ceil(len(images) / cols)
    per_sheet = max(len(g) for g in groups)

    # Size the cell so neither side of the canvas exceeds Telegram's ceiling,
    # and never upscale past the source resolution — with only a handful of
    # films a full-width canvas would blow 500px posters into soft, bloated PNG.
    by_width = (MAX_LONG_SIDE - 2 * MARGIN - (cols - 1) * GUTTER) / cols
    by_height = (MAX_LONG_SIDE - 2 * MARGIN - (rows - 1) * GUTTER) * cell_ratio / rows
    native = statistics.median(widths) if widths else by_width
    cell = (int(min(by_width, by_height, native)), cell_ratio)

    if len(groups) == 1:
        return [build_collage(groups[0], base, cols, rows, cell, 1)]
    sheets, first = [], 1
    for i, group in enumerate(groups, 1):
        sheets.append(build_collage(group, base.with_name(f"{base.stem}-{i}{base.suffix}"),
                                    cols, rows, cell, first))
        first += len(group)
    return sheets


def build_collage(images: list, path: Path, cols: int, rows: int,
                  cell: tuple[int, float], start: int = 1) -> tuple[Path, int, int]:
    """Uniform grid, every poster scaled to fit (never cropped) and centred.

    Posters share a width of 500 but vary in height, so the cell takes the
    median aspect ratio: most fill it edge to edge and the odd one out gets
    equal bands on both sides.

    Every sheet keeps the full grid's canvas so the album stays uniform, which
    means the last one is usually short. Its rows come from row_plan() and each
    is centred, with the block centred vertically, so any remainder reads as
    deliberate rather than truncated.
    """
    n = len(images)
    cell_w, cell_ratio = cell
    cell_h = int(cell_w / cell_ratio)  # floor, so the canvas never exceeds the ceiling

    width = 2 * MARGIN + cols * cell_w + (cols - 1) * GUTTER
    height = 2 * MARGIN + rows * cell_h + (rows - 1) * GUTTER

    # Work out every poster's box first: the backdrop needs them to place its
    # wash, and the shadows all go down in one pass so none falls on a neighbour.
    plan = row_plan(n, cols, rows)
    y_pad = (rows - len(plan)) * (cell_h + GUTTER) // 2
    boxes = []
    for r, in_row in enumerate(plan):
        offset = (cols - in_row) * (cell_w + GUTTER) // 2
        for c in range(in_row):
            boxes.append((MARGIN + offset + c * (cell_w + GUTTER),
                          MARGIN + y_pad + r * (cell_h + GUTTER)))

    canvas = backdrop(images, (width, height), boxes, cell_w, cell_h)

    shade = Image.new("L", (width, height), 0)
    pen = ImageDraw.Draw(shade)
    for x0, y0 in boxes:
        pen.rectangle([x0 - 4, y0 - 2, x0 + cell_w + 4, y0 + cell_h + 8], fill=150)
    shade = shade.filter(ImageFilter.GaussianBlur(14))
    canvas = Image.composite(Image.new("RGB", (width, height), (0, 0, 0)), canvas, shade)

    for (x0, y0), im in zip(boxes, images):
        if im is None:
            canvas.paste(PLACEHOLDER, (x0, y0, x0 + cell_w, y0 + cell_h))
            continue
        fitted = ImageOps.contain(im, (cell_w, cell_h), Image.LANCZOS)
        canvas.paste(fitted, (x0 + (cell_w - fitted.width) // 2,
                              y0 + (cell_h - fitted.height) // 2))

    draw_numbers(canvas, boxes, start, cell_w, plan)

    canvas.save(path, "PNG", optimize=True)
    if path.stat().st_size > PHOTO_BYTES_LIMIT:
        path = path.with_suffix(".jpg")
        canvas.save(path, "JPEG", quality=88, optimize=True)
    return path, cols, rows


# ---------------------------------------------------------------------- message


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def tg_len(text: str) -> int:
    """Visible length as Telegram measures it: tags stripped, UTF-16 units."""
    visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(visible.encode("utf-16-le")) // 2


def number_glyph(n: int) -> str:
    """1 as 𝟭 — mathematical sans-serif bold digits.

    Built a digit at a time, so unlike the circled forms (which run out at 20
    or 35) any number works. <b> was no use here: the multi-venue headings are
    bold already and it vanished into them.
    """
    return "".join(chr(0x1D7EC + int(d)) for d in str(n))


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def fmt_span(start: dt.date, end: dt.date) -> str:
    return f"{start:%-d %b}" if start == end else f"{start:%-d %b} – {end:%-d %b}"


def render_empty(start: dt.date, end: dt.date) -> str:
    span = f"from {start:%-d %b}" if end == dt.date.max else fmt_span(start, end)
    return ("<b>🇯🇵 Japanese in Berlin cinemas</b>\n"
            f"No screenings listed {span}.")


def organise(movies: list[dict]):
    """Lay the post out so every film is named exactly once.

    Films playing at several venues come first, each heading its own block of
    cinemas — listing them under every venue would repeat them. Whatever is
    left plays at a single cinema, so those group under the venue instead.

    Returns (multi, cinemas, order); `order` is the flat display sequence, which
    the collage follows so poster and line agree.
    """
    multi, single = [], []
    for m in movies:
        venues: dict[str, dict] = {}
        for s in m["screenings"]:
            v = venues.setdefault(s["cinema"], {"district": s["district"], "dates": []})
            v["dates"].append(dt.date.fromisoformat(s["start"][:10]))
        entries = sorted(
            ((c, v["district"], sorted(set(v["dates"]))) for c, v in venues.items()),
            key=lambda t: (t[2][0], t[0]),
        )
        (multi if len(entries) > 1 else single).append((m, entries))

    multi.sort(key=lambda t: (-len(t[1]), t[1][0][2][0], t[0]["title"]))

    groups: dict[str, dict] = {}
    for m, entries in single:
        cinema, district, dates = entries[0]
        g = groups.setdefault(cinema, {"district": district, "films": []})
        g["films"].append((m, dates))
    cinemas = [
        (cinema, g["district"], sorted(g["films"], key=lambda p: (p[1][0], p[0]["title"])))
        for cinema, g in groups.items()
    ]
    cinemas.sort(key=lambda t: (-sum(len(d) for _, d in t[2]), t[0]))

    order = [m for m, _ in multi] + [m for _, _, films in cinemas for m, _ in films]
    return multi, cinemas, order


def venue_label(cinema: str, district: str, level: int) -> str:
    return esc(cinema) + (f", {esc(district)}" if district and level < 1 else "")


def title_of(movie: dict, level: int) -> str:
    title = movie["title"]
    if level >= 3 and len(title) > 30:
        title = title[:29].rstrip(" -:,") + "…"
    return esc(title)


def film_entry(movie: dict, level: int, number: int) -> str:
    return f'{number_glyph(number)} · <a href="{esc(movie["url"])}">{title_of(movie, level)}</a>' 


def render(multi, cinemas, events: dict, start: dt.date, end: dt.date, level: int,
           films: int, shows: int, dropped: int = 0) -> str:
    header = (
        f"<b>🇯🇵 Japanese in Berlin cinemas</b>\n"
        f"{plural(films, 'film')} · {plural(shows, 'screening')} · {fmt_span(start, end)}\n"
    )

    # The same numbering the collage uses, so poster 7 is line 7.
    order = [m for m, _ in multi] + [m for _, _, films in cinemas for m, _ in films]
    number = {m["id"]: i for i, m in enumerate(order, 1)}

    blocks = []
    for movie, entries in multi:
        venues = "\n".join(
            f"▸ {venue_label(cinema, district, level)}"
            for cinema, district, dates in entries
        )
        blocks.append(f'{number_glyph(number[movie["id"]])} · '
                      f'<b><a href="{esc(movie["url"])}">{title_of(movie, level)}</a></b>\n{venues}')
    for cinema, district, entries in cinemas:
        lines = "\n".join(film_entry(m, level, number[m["id"]]) for m, _ in entries)
        blocks.append(f"<b>{venue_label(cinema, district, level)}</b>\n{lines}")
    if dropped:
        blocks.append(f'<a href="{ovb.INDEX_URL}">+{dropped} more on ov-berlin.info</a>')
    body = "\n\n".join(blocks)

    n = events.get("count")
    if events.get("items"):
        shown = ", ".join(f'<a href="{esc(u)}">{esc(t)}</a>' for u, t in events["items"][:5])
        footer = f'\n\n🎪 <a href="{esc(EVENTS_URL)}">Festivals &amp; events</a>: {shown}'
    elif n:
        footer = f'\n\n🎪 <a href="{esc(EVENTS_URL)}">Festivals &amp; events</a>: {n} listed'
    else:
        footer = f'\n\n🎪 <a href="{esc(EVENTS_URL)}">Festivals &amp; events</a> — none listed'
    return header + "\n" + body + footer


def render_fitting(movies, events, start, end) -> tuple[str, int]:
    """Render at the richest detail level that still fits in one message."""
    shows = sum(len(m["screenings"]) for m in movies)
    for level in range(2):  # 0: with districts, 1: without
        multi, cinemas, order = organise(movies)
        text = render(multi, cinemas, events, start, end, level, len(movies), shows)
        if tg_len(text) <= TEXT_LIMIT:
            if level:
                print(f"note: dropped districts to fit {TEXT_LIMIT} chars", file=sys.stderr)
            return text, level, order

    # Still too long: drop whole films rather than slicing the string, which
    # would cut an <a> tag in half and make Telegram reject the message. They
    # go from the tail of the display order, so the collage stays a prefix.
    kept = organise(movies)[2]
    while len(kept) > 1:
        kept.pop()
        multi, cinemas, order = organise(kept)
        text = render(multi, cinemas, events, start, end, 1, len(movies), shows,
                      dropped=len(movies) - len(kept))
        if tg_len(text) <= TEXT_LIMIT:
            print(f"warning: dropped {len(movies) - len(kept)} films to fit "
                  f"{TEXT_LIMIT} chars", file=sys.stderr)
            return text, 1, order
    multi, cinemas, order = organise(kept)
    return render(multi, cinemas, events, start, end, 1, len(movies), shows,
                  dropped=len(movies) - len(kept)), 1, order


# ---------------------------------------------------------------------- telegram


def tg_call(token: str, method: str, fields: dict, files: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                     f"{value}\r\n").encode()
        for key, (filename, blob) in files.items():
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
                     f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
            body += blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        data = bytes(body)
    else:
        data = urllib.parse.urlencode(fields).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def send_images(token: str, chat: str, paths: list[Path], album: bool = True) -> dict:
    """All the sheets as one album. Telegram caps a media group at 10 items.

    The listing goes out as its own message afterwards rather than as a caption:
    a caption is limited to 1024 characters and the listing needs more.
    With album=False each sheet becomes its own message instead — no mosaic, so
    nothing is cropped, at the cost of one bubble per image.
    """
    if len(paths) == 1 or not album:
        result = {"ok": True}
        for p in paths:
            result = tg_call(token, "sendPhoto", {"chat_id": chat},
                             {"photo": (p.name, p.read_bytes())})
            if not result.get("ok"):
                return result
        return result

    result = {"ok": True}
    for batch in (paths[i:i + 10] for i in range(0, len(paths), 10)):
        media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(batch))]
        files = {f"p{i}": (p.name, p.read_bytes()) for i, p in enumerate(batch)}
        result = tg_call(token, "sendMediaGroup",
                         {"chat_id": chat, "media": json.dumps(media)}, files)
        if not result.get("ok"):
            return result
    return result


def send(token: str, chat: str, text: str, collages: list[Path], album: bool = True) -> int:
    photo = send_images(token, chat, collages, album)
    if not photo.get("ok"):
        print(f"error: sending {len(collages)} image(s) failed: "
              f"{photo.get('description')}", file=sys.stderr)
        return 1

    message = tg_call(token, "sendMessage", {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    })
    if not message.get("ok"):
        print(f"error: sendMessage failed: {message.get('description')}", file=sys.stderr)
        return 1
    print(f"sent to {chat}: {len(collages)} image(s) + {tg_len(text)}-char message")
    return 0


# -------------------------------------------------------------------------- main


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(
        description="Japanese screenings in Berlin as a Telegram post.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1],
    )
    p.add_argument("--send", action="store_true", help="post to Telegram (else dry run)")
    p.add_argument("--lang", default="Japanese", help="spoken language (default Japanese)")
    p.add_argument("--no-blacklist", action="store_true",
                   help=f"ignore {BLACKLIST_FILE.name}")
    p.add_argument("--loose", action="store_true",
                   help="also keep films that merely list the language (Wings of Desire etc.)")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                   help="first day to include (default tomorrow)")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last day to include")
    p.add_argument("--collage", type=Path, default=HERE / "collage.png")
    p.add_argument("--chunk", type=int, default=6, metavar="N",
                   help="films per image (default 6); 0 for a single sheet")
    p.add_argument("--separate", action="store_true",
                   help="send sheets as separate photos, not an album (never cropped)")
    p.add_argument("--refresh", "-r", action="store_true", help="bypass the cache")
    p.add_argument("--ttl", type=int, default=ovb.DEFAULT_TTL, metavar="SEC")
    args = p.parse_args()

    movies, start, end = collect(args)
    if movies:
        # Render first: it settles the display order, and the collage follows it
        # so the nth poster is the nth film in the message.
        events = parse_events(ovb.fetch(EVENTS_URL, args.ttl, args.refresh))
        text, level, order = render_fitting(movies, events, start, end)

        posters = list(
            concurrent.futures.ThreadPoolExecutor(max_workers=8).map(
                lambda m: fetch_poster(m.get("poster", ""), args.ttl, args.refresh), order
            )
        )
        sheets = build_collages(posters, args.collage, args.chunk, album=not args.separate)

        missing = sum(1 for x in posters if not x)
        print(f"{plural(len(movies), 'film')} · "
              f"{plural(sum(len(m['screenings']) for m in movies), 'screening')} · {start} – {end}" + (f", {missing} posters missing" if missing else ""),
              file=sys.stderr)
        for path, cols, rows in sheets:
            w, h = Image.open(path).size
            print(f"collage {cols}x{rows} → {path.name} {w}x{h} "
                  f"(aspect {w / h:.2f}, {path.stat().st_size // 1024} KB)", file=sys.stderr)
        print(f"message {tg_len(text)}/{TEXT_LIMIT} chars, detail level {level}", file=sys.stderr)
    else:
        # No films this week — still deliver a message saying exactly that.
        text, sheets = render_empty(start, end), []
        print(f"no {args.lang} screenings from {start} — sending empty notice",
              file=sys.stderr)

    if not args.send:
        print(text)
        return 0

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("error: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see .env)")
    return send(token, chat, text, [p for p, _, _ in sheets], album=not args.separate)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
