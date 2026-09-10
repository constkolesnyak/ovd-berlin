# ovd-berlin

**Berlin cinema, in the original language.** Berlin shows hundreds of films a week in their original
audio with subtitles — but the only listing site that tracks it has no way to ask *"Japanese films
with English subtitles, next week, not at the multiplex"*. This does.

- **`ovb.py`** — terminal CLI: original-language screenings in Berlin, filtered by spoken language, subtitle version, date, cinema and rating.
- **`report.py`** — the same data as a Telegram post: a poster collage plus one message. See [Telegram report](#telegram-report).

Pure standard library — no pip install, no virtualenv, no dependencies. Two files, ~1,500 lines.

```console
$ ./ovb.py --lang japanese -d 7
Japanese · English subtitles · 10 Sep – 16 Sep 2026 · 7 movies, 20 screenings

Thu 10 Sep
  20:00  Paprika (2006)
         Babylon Alexanderplatz, Mitte  imdb 7.7  lb 4.1  rt 87%
  21:00  Exit 8 (2025)
         Rollberg Kinos, Neukölln  imdb 6.5  lb 3.1  rt 91%

Fri 11 Sep
  17:30  Akira (1988)
         Babylon Alexanderplatz, Mitte  imdb 8.0  lb 4.3  rt 91%
  20:00  Der Himmel über Berlin (1987)
         Babylon Alexanderplatz, Mitte  imdb 7.9  lb 4.3  rt 95%
  22:00  A Page of Madness (1926)
         Babylon Alexanderplatz, Mitte  imdb 7.3  lb 3.8  rt 0%
  ...
```

## How the data is obtained

The site publishes RSS feeds (`/movies.rss`, `/movies/open-air.rss`, `/movies/classic.rss`, `/movies/coming-soon.rss`), but they carry **no language or subtitle information** — only title, link and blurb. So this script reads two other things instead:

- the `/movies/` index page, where each movie card carries `data-languages`, `data-versions` and per-day `data-day-id` rows — enough to decide *which* movies are worth a closer look, in one request;
- the JSON-LD `ScreeningEvent` blocks on each surviving movie's page, which give exact showtime, cinema, district, subtitle version and ticket link.

Responses are cached in `.cache/` for 6 hours.

The same index cards also carry IMDb, Letterboxd and Rotten Tomatoes scores as data attributes, so `--min-imdb` and `--sort rating` cost no extra requests.

## Usage

```sh
./ovb.py --lang japanese              # Japanese audio + English subtitles
./ovb.py -l japanese -d 5             # just the next 5 days
./ovb.py -l korean -s any             # any subtitle version, not only English
./ovb.py -l japanese --group movie --sort rating --links
./ovb.py --min-imdb 8 --genre horror  # any language, English subs
./ovb.py -c babylon -c rollberg       # only these cinemas (substring match)
./ovb.py --from 2026-08-01 --to 2026-08-07
./ovb.py -l japanese --json | jq '.[] | .cinema' | sort | uniq -c
```

Subtitle versions follow the site's own vocabulary:

| `--subs` | meaning |
| --- | --- |
| `omeu` *(default)* | original audio, English subtitles |
| `omu` | original audio, German subtitles |
| `ov` | original audio, no subtitles |
| `any` | everything (the version is then printed next to each screening) |

Discover what to filter on:

```sh
./ovb.py --list langs      # spoken languages in Berlin cinemas now, by movie count
./ovb.py --list genres
./ovb.py --list cinemas    # all 65 venues the site tracks
```

## Notes

- **How far ahead the data goes is the site's limit, not the script's.** ov-berlin.info publishes about 15 days, and only the first ~9 are dense: Berlin cinemas announce their programme one Thursday-to-Wednesday week at a time, so the tail fills in as the week turns. By default the script shows everything that's listed; `--days`/`--to` only narrow that.
- `--lang` matches any language *listed* for a movie, so a multi-language film shows up under each of them — e.g. `Der Himmel über Berlin` lists Japanese among seven languages. Use `--not-lang german` or `--title` to narrow.
- Filtering happens in two stages: cheap index-level filters run first, and only the movies that pass get their page fetched (6 threads). A narrow query costs a handful of requests.
- `--refresh` bypasses the cache, `--ttl 0` refetches everything once, `rm -rf .cache` starts clean.
- Exit code is `1` when nothing matches, so it composes in shell pipelines.

## Telegram report

`report.py` imports `ovb.py` and reuses its fetching, caching and parsing, then adds the parts a chat post needs. Defaults: Japanese, **from tomorrow** (computed at run time), any subtitle version, no end date.

```sh
./report.py                      # dry run: print the message, write collage.png
./report.py --send               # post it
./report.py --lang Korean --from 2026-08-01 --to 2026-08-14
./report.py --loose              # keep films that merely list the language
./report.py --no-blacklist       # include excluded venues
```

Venues you never want to see go in **`cinema-blacklist.txt`**, one name per line, `#` for comments, matched case-insensitively against the full name (`./ovb.py --list cinemas` prints them all). Their screenings are dropped before anything else, so a film that plays *only* at a blacklisted venue disappears from the listing and the collage together. Each run reports on stderr how many screenings were dropped, and names any blacklist entry that matched nothing — which is how you catch a typo.

Credentials come from the environment or a `.env` next to the script (gitignored, `chmod 600`):

```sh
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
```

<img src="docs/telegram-collage.jpg" alt="One sheet of the poster collage sent to Telegram" width="330" align="right">

### Why it looks the way it does

- **A bare photo, then one text message.** The collage goes out with no caption; the listing follows as a single message. Telegram caps a caption at 1024 characters and a message at 4096, so the two cannot be merged anyway.
- **Every film is named exactly once.** Films playing at several venues come first, each heading its own block of cinemas (`· Venue — dates`) — listing them under every venue would repeat them. Everything left plays at a single cinema, so those group under the venue instead (`▸ Title — dates`), busiest venue first. No year, rating, subtitle version or showtimes — for those, tap the title. The whole post lands around 1250 characters.
- **The collage follows the message.** The layout is settled before any poster is fetched, so the nth poster is the nth film you read.
- **One album, then one message.** All the poster sheets go out as a single media group, the listing follows as its own message. The listing cannot ride along as the album's caption: a caption is capped at 1024 characters and the listing runs past that (the API answers `Bad Request: message caption is too long`). `--separate` sends each sheet as its own photo message instead.
- **Nine films per sheet, the remainder on the last** (`--chunk`, `0` for a single sheet). Every sheet keeps the full grid's canvas so the album stays uniform, and a short last sheet is centred on both axes — full rows first, a short row centred beneath them, the block centred vertically. Splitting also buys resolution: one sheet of everything hits the 2560px canvas ceiling and each poster lands around 283px, whereas nine per sheet lets every cell render at its native 500px — the largest the site serves, as `?w=`, `@2x` and `/original` all fail.
- **The background is a blurred wash of the sheet's own posters**, laid out on the same grid, cycled to cover cells the sheet does not fill, then pushed towards black by `BACKDROP_DIM`. Each poster sits over its own colours, and — the point of it — the empty space on a short last sheet has something behind it, so the gap reads as part of the design rather than a missing tile. Posters get a soft drop shadow on top of that; the shadows are drawn in one pass so none falls across a neighbour.
- **Telegram will crop the album tiles, and portrait sheets accept that.** Its grouped-media layout ([`grouped_layout.cpp`](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/ui/grouped_layout.cpp)) sorts images into wide (ratio > 1.2), narrow (< 0.8) and square, picks an arrangement from those classes, then crops each image to fill its tile; the Bot API exposes no control over any of it. Portrait sheets land in the narrow class and get the lopsided arrangements, so the collapsed album preview is not as tidy as squared-off sheets would make it. That is a deliberate trade: 0.70 is what a phone screen wants, squaring off is not, and tapping any tile shows the sheet whole and uncropped anyway.
- **It degrades before it breaks.** If the listing ever exceeds 4096, districts go first, then whole films from the tail of the display order — which keeps the collage a prefix of the message — with a `+N more` link. The text is never sliced mid-tag, which would make Telegram reject the message outright.
- **`--lang Japanese` means Japanese cinema, not "has Japanese in it".** A film qualifies if the language leads its list *or* the country of origin matches. Both rules are needed: *Wings of Desire* and *Marty Supreme* merely contain Japanese dialogue, while *The Wind Rises* is Ghibli but lists Japanese last. Skipped films are reported on stderr; `--loose` keeps them.
- **The collage is portrait, and nothing in it is cropped.** Posters share a width of 500px but vary in height, so the cell takes the median aspect ratio and each poster is scaled to *fit* and centred. Column count targets a 0.62 aspect — tall, to fill a phone screen — while leaving few holes; a short final row is centred. Neither side exceeds 2560px, the point past which Telegram re-encodes, and posters are never upscaled past their source resolution. It is uploaded as a lossless PNG so the only compression is Telegram's own.

### Caveat: events

The `/events` section is folded into the report, but it has been empty site-wide since this was written (0 events in all seven cities). The parser recognises the empty state and reads the counter; **the populated markup is unverified** — when real events appear, check that section against the live HTML before trusting it.

## License

[MIT](LICENSE). Screening data belongs to ov-berlin.info; posters to their respective rights holders.
