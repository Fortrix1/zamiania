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
