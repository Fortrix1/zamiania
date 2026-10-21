# Bot review — why the Download button stopped working

## Root cause
The button/handler logic in `bot.py` is fine (verified by tracing the
`dl:<sid>:<idx>` callback flow end to end). The failure is in the
download pipeline underneath it:

1. **xHamster started ciphering video URLs in the player config.**
   Confirmed upstream: yt-dlp issue #15239 (Dec 2025) — "failed to
   decipher URL / No video formats found". That means `xh.sources()`
   either finds nothing usable in `window.initials`, or returns
   ciphered URLs that 403.
2. **The yt-dlp fallback can't save it either**, because
   `requirements.txt` installs the *stable* release, which lags weeks
   behind these site changes. When the fallback also fails the user just
   gets "No downloadable source found" — the button appears to do
   nothing.
3. **No validation anywhere:** a 403 HTML error page used to be saved
   as `/tmp/<id>.mp4` and sent to the user as a "video".

## Bot-side bugs found (also fixed)
- `pick_under()`: with no size metadata, `est() == 0` counted as
  "fits 400MB", so it picked 1080p — often a 1-2 GB file that blows the
  upload limit or stalls forever.
- `_ytdlp_url()`: rewrote `/videos/12345` to `/videos/video-12345`
  (404). Removed — yt-dlp handles the real URL forms.
- Raw-HTML fallback truncated signed URLs at `\u0026` (&), killing the
  token query param -> 403. Now unescaped.
- No feedback message while downloading; errors only visible in CI logs.
  `deliver()` now posts a "Fetching..." status and edits it with the
  outcome (or deletes it on success).
- CI never updated yt-dlp. `bot.yml` now force-reinstalls yt-dlp from
  the master branch (nightly) on every run.

## What changed in the fixed files
- `deliver()`: 4-stage pipeline (cache -> direct URL -> HLS -> yt-dlp)
  with per-stage isolation, `is_probably_video()` magic-byte check, and
  a status message the user can actually see.
- `download()`: fails fast on HTML/JSON responses and short timeouts.
- `pick_under()`: known-size fits preferred; otherwise smallest known;
  all-unknown -> 720p-first ordering.
- `xhamster.py`: `\u0026`/`\u003D` unescaping, quote-safe regex.
- `bot.yml`: nightly yt-dlp reinstall step.

## Round 2 (2026-10-21): MissingSchema — Invalid URL '073b79c7d...'

That hex blob is xHamster's new CIPHERED video URL (same obfuscation as
yt-dlp issue #15239). Root hole: `xhamster._walk()` collected ANY string
found in a JSON `url` field with no scheme check, so ciphered tokens
became "variants" and were fed straight to `requests`.

Fixes:
- `_walk()` now only collects strings starting with http://, https:// or //
  (new `_is_dl_url()` helper), with the same filter inside `sources()`.
- `download()` raises a clear error if the URL has no scheme.
- `deliver()`: the `xh.sources()` call and the yt-dlp stage are each
  wrapped in their own try — a failure in one stage now falls through to
  the next instead of crashing to the user-facing handler.
- Cached `InputDocument` re-send failures other than
  FileReferenceExpiredError now drop the cache entry and continue.

With ciphered URLs everywhere, the pipeline intentionally degrades to the
yt-dlp stage — that is the only maintained decipherer. If yt-dlp nightly
also fails, the bot now reports a clean "No downloadable source found" with
the DEBUG dump instead of a raw MissingSchema traceback.

## Round 3 (2026-10-21): admin panel, stats, query log + click menu

New features (all in `bot.py`, state persists in `data/stats.json`,
pushed by the existing cache step in `bot.yml`):

- **Click menu**: `/start` now shows a persistent ReplyKeyboard
  (\U0001F50D Search / \u2139\uFE0F Help). "Search" works from any state;
  no need to type /search every time. `/search` and `/start` still work.
- **Stats tracking**: every interaction records the user
  (name, use count, first/last seen) into `STATS["users"]`.
- **Query log**: every search records query + sort + user + timestamp
  (last 500 kept) into `STATS["queries"]`.
- **Admin panel**: set the `ADMIN_ID` repo secret to your Telegram user
  id (get it from @userinfobot). Then `/admin`, `/panel`, `/stats`,
  `/query` or `/queries` opens an inline panel with four views:
  \U0001F4CA user stats (total / active today / active week / searches)
  \U0001F50D recent searches (who searched what, when)
  \U0001F3C6 top queries
  \U0001F465 last 20 active users
  Only `ADMIN_ID` can open it; everyone else is silently ignored.
- Stats flush every 30s in the background and force-flush on shutdown,
  so the scheduled runner's git-push step persists them.
