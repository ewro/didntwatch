# tldw — too long; didn't watch

A [Claude Code](https://claude.com/claude-code) **Agent Skill** that summarizes a
YouTube video from its transcript (subtitles). Give Claude a YouTube link — in any
common format — or a bare video id, and get a summary without watching the video.
The transcript then stays in the conversation, so you can ask follow-up questions,
pull quotes, or switch the summary format.

Hand it **a whole list** of links instead — or a bookmarks file — and it switches
to **batch mode**: it fetches every transcript in one polite pass and builds a
single numbered markdown report with a short summary per video.

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)

## What it does

- Accepts every common YouTube link form (`watch?v=`, `youtu.be/`, `/shorts/`,
  `/embed/`, `/live/`, extra params like `&t=`) or a bare 11-character id.
- Pulls the transcript via a bundled [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
  toolchain with the [bgutil PO-token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider),
  browser cookies and TLS impersonation — including the list of available tracks
  and per-segment timestamps.
- Lets you choose the **output format** (key theses by default), **language**, and
  **length** of the summary.
- **Analyzes viewer comments on request** — top threads with uploader
  replies/hearts, fetched through the same toolchain (no YouTube API key):
  corrections and pushback, dominant themes, what the author confirmed.
- Handles the awkward cases honestly: no subtitles, captions disabled, access
  blocked/rate-limited, private/removed videos — and always offers a
  **manual-paste fallback** (you paste the transcript text, Claude summarizes that).
- **Batch mode** for many videos at once: fetches the whole list in one throttled,
  resumable pass (concurrency limit, delay, exponential backoff on rate-limits) and
  produces a numbered report — without ever loading every transcript into context.
- **Never invents content.** The summary is grounded only in the transcript or the
  text you paste.

## How it compares

Plenty of tools turn a YouTube link into a summary — from one-shot transcript
skills to [fabric](https://github.com/danielmiessler/fabric)'s pattern pipelines.
The difference is everything that happens around that:

| | Typical transcript skill | fabric (`-y` + pattern) | **tldw** |
|---|---|---|---|
| **Transcript fetching** | `youtube-transcript-api` — breaks against YouTube's PO-token wall — or a paid transcript API | system `yt-dlp` you install yourself; no PO-token provider, VTT only | ✅ self-provisioned `yt-dlp` + `bgutil` PO-token provider + browser cookies + TLS impersonation — survives YouTube's 2025–26 hardening |
| **The summarizer** | a fixed prompt over a cheap API model, one shot | any provider incl. local models, pattern library + reasoning strategies — still a one-shot text→text pass | ✅ the agent itself — the summary is just the *first* turn of a conversation |
| **After the summary** | done | stateless: re-run the pipeline for every question | ✅ the transcript stays in context: follow-up Q&A, quotes, timestamps, drill-down — no re-fetch |
| **Failure handling** | "couldn't fetch" | error strings | ✅ an eight-status taxonomy (`ok`, `no_transcript`, `blocked`, …), each mapped to a distinct behavior, plus a manual-paste fallback |
| **Memory** | none | none | ✅ offline cache + index: `find` recalls any seen video by title *or topic words inside the transcript*, months later, zero network |
| **Batch / scale** | none | a shell loop you write yourself | ✅ throttled resumable fetch pass → slim manifest → map-reduce across sub-agents → one numbered report, built around the context window |
| **Languages** | whatever YouTube hands back, machine translations included | language fallback can silently pull a machine translation | ✅ real tracks only (author-uploaded > original auto captions); the model translates while summarizing |
| **Beyond the transcript** | — | comments & metadata (needs a YouTube API key), ✅ frame extraction via OCR/FFmpeg | ✅ comments with **no API key** (top threads, uploader replies/hearts, total count); no frame extraction |
| **Epistemics** | summary and opinion blended | depends on the pattern | ✅ a strict contract: the summary is transcript-only; commentary is opt-in, clearly labeled, grounded with web checks |
| **Environment & cost** | a browser extension or SaaS subscription | any shell on any OS; $0 with local models | any agentic LLM harness (the engine is a standalone CLI; the skill manifest is a thin adapter) — first-class in **Claude Code**, best with a frontier model; Linux/macOS today |

## How it works

The skill is deliberately lightweight, with a clean split of responsibilities:

| Part | Responsibility |
|------|----------------|
| `scripts/transcript.py` | The *only* hard job — turn a URL/id into a clean transcript (text + timestamps + metadata) and print structured JSON. `batch` fetches many at once into the cache and writes a slim manifest. **No summarizing.** Caches each fetch to `.cache/`. |
| `scripts/transcript.sh` | Self-provisioning bootstrap: on first run downloads the yt-dlp standalone build and the bgutil PO-token provider into `.runtime/` (plus a local Node runtime if the system lacks node ≥ 18), then execs the fetcher. |
| **Claude** | Everything "smart": the summary, follow-up Q&A, quotes, re-formatting, translation, and (in batch mode) building the report from the cache — all on top of the transcripts the script returns. |

A summary is just the default *first* action. Because the transcript lands in the
conversation (and on disk), follow-up questions are answered without hitting
YouTube again.

**Two modes, opposite context contracts.** In **single mode** the full transcript
lands in Claude's context (and on disk), so you can keep talking about the video.
In **batch mode** transcripts are *deliberately kept out* of context — they
wouldn't fit for ~100 videos — so the script caches them and returns only a light
manifest; Claude builds the report over the cache (ideally fanning out to
sub-agents per chunk). The two dock through `.cache`: after a batch report you can
still drill into any single video by reading its cached transcript.

## Requirements

- **Claude Code** (the skill runs inside it).
- **Python 3.9+** (standard library only — no pip packages).
- **`curl` and `tar`** for the first-run toolchain download.
- Network access to YouTube (the script fetches subtitles directly).
- A browser with a YouTube login — cookies are what get past the bot wall
  (Firefox by default; override with `TLDW_COOKIES_FROM_BROWSER` /
  `TLDW_COOKIES_FILE`).

The fetching toolchain provisions itself into `.runtime/` on first run: the
yt-dlp standalone build (~35 MB), the bgutil PO-token provider (built with
`npm install` + `tsc`), and a local Node runtime (~30 MB) only if the system
has no `node` ≥ 18. No manual setup needed.

## Installation

The repository root *is* the skill. Claude Code discovers skills in
`~/.claude/skills/`, so install by cloning there:

```bash
git clone https://github.com/<you>/tldw.git ~/.claude/skills/tldw
```

Or keep it in a dev folder and symlink it (edits stay in sync):

```bash
git clone https://github.com/<you>/tldw.git ~/dev/tldw
ln -s ~/dev/tldw ~/.claude/skills/tldw
```

That's it. The fetching toolchain bootstraps itself the first time the skill runs
(expect a one-time download on the first call). Restart Claude Code (or start a
new session) so it picks up the new skill.

## Usage

In any Claude Code session, just share a video and ask for a summary — the skill
activates automatically (triggers work in English and Russian), or invoke it
explicitly with `/tldw`.

```
Summarize this video: https://youtu.be/8jPQjjsBbIc
```

```
tldw https://www.youtube.com/watch?v=8jPQjjsBbIc — key points in Russian, short
```

```
Recap https://youtu.be/8jPQjjsBbIc by sections with timestamps
```

For a **list of videos**, give several links (or a bookmarks file) and ask for a
report — the skill switches to batch mode automatically:

```
Here's my bookmarks file — pull every YouTube link from the "Watch later" folder
and give a short summary of each, save to yt_bookmarks_summary.md with numbering.
```

### Output formats (you pick; default = **key theses**)

- **Short summary** — a few sentences capturing the gist.
- **Key theses** — a bulleted list of the main points *(default)*.
- **Sectioned recap with timecodes** — topical sections, each with an `mm:ss` and a
  deep link like `https://youtu.be/<id>?t=<seconds>`.

You can also set the summary **language** (defaults to the language you wrote in,
else the transcript language) and **length** (default: medium).

### Manual-paste fallback

If a transcript can't be fetched (no captions, access blocked, etc.), paste the
transcript text yourself and ask for a summary — Claude summarizes the pasted text
directly, with the same no-fabrication rule.

## The transcript CLI (optional, standalone)

You can run the fetcher directly; it prints JSON and never summarizes.

```bash
# list available subtitle tracks
scripts/transcript.sh list "https://youtu.be/8jPQjjsBbIc"

# fetch the best real track (author-uploaded subtitles if any, else original auto captions)
scripts/transcript.sh fetch "https://youtu.be/8jPQjjsBbIc"

# prefer a specific language among the video's REAL tracks (never machine translations;
# falls back to the best real track with `lang_fallback: true`)
scripts/transcript.sh fetch "8jPQjjsBbIc" --lang ru

# viewer comments: top threads + uploader replies/hearts, no API key
# (comment_count = the video's total, fetched_count = what you got)
scripts/transcript.sh comments "8jPQjjsBbIc" --max 100 --sort top

# batch: many videos into the cache + a manifest (slim JSONL on stdout)
scripts/transcript.sh batch --input urls.txt --manifest manifest.json --lang ru
scripts/transcript.sh batch "<url1>" "<url2>" ...   # ids/urls as args too

# recall from the cache — offline, no network or runtime needed
scripts/transcript.sh find "родительское поведение страхи"   # find a cached transcript by id/url/title/topic
scripts/transcript.sh get  "8jPQjjsBbIc" --lang ru           # print a cached transcript's text (--json for full record)
scripts/transcript.sh reindex                                # rebuild .cache/index.json from the cache
```

`find` matches the query against cached **titles/authors** first, then falls back
to scanning transcript **bodies**, ranking by how many distinct query words a
video covers and returning a confirming `snippet`. It reads only `.cache` (via
`.cache/index.json`, maintained automatically on every fetch), so drilling into a
video you've already summarized never re-hits YouTube.

`fetch` output (truncated):

```json
{
  "status": "ok",
  "video_id": "8jPQjjsBbIc",
  "url": "https://youtu.be/8jPQjjsBbIc",
  "title": "How to stay calm when you know you'll be stressed | Daniel Levitin | TED",
  "author": "TED",
  "language_code": "en",
  "is_generated": false,
  "available_tracks": [ { "language": "English", "language_code": "en", "...": "..." } ],
  "segment_count": 260,
  "text": "A few years ago, I broke into my own house...",
  "segments": [ { "start": 13.24, "dur": 2.56, "text": "A few years ago,..." } ],
  "cache_file": ".cache/8jPQjjsBbIc.en.json"
}
```

### Batch mode (`batch`)

`batch` fetches a whole list in one polite pass and is built for scale: it streams
one **slim** JSON record per video to stdout (one per line — no `text`/`segments`,
so stdout stays small on hundreds of videos) and keeps every full transcript only
in `.cache/`. With `--manifest <path>` it also writes the complete array as the
index Claude builds the report from.

```bash
scripts/transcript.sh batch --input urls.txt --manifest manifest.json --lang ru
```

| Flag | Default | What it does |
|------|---------|--------------|
| `--input <file>` | — | One URL/id per line; blank lines and `#` comments ignored. |
| *(positional)* | — | URLs/ids as arguments, combined with `--input`. Duplicate ids are de-duplicated. |
| `--lang <code>` | — | Preferred language among each video's real subtitle tracks (never machine translations). |
| `--manifest <path>` | — | Write a JSON array of all records (the report index). |
| `--concurrency <n>` | `2` | Parallel requests. |
| `--delay <sec>` | `1.0` | Pause between requests. |
| `--max-retries <n>` | `3` | Exponential backoff with jitter on `blocked`. |
| `--refresh` | off | Re-fetch even when a valid cache file already exists. |

- **Resumable / idempotent.** By default, videos already cached in the requested
  language are served from disk (no network); re-running after a partial failure
  fetches only what's missing.
- **Polite, and gives up gracefully.** If many requests are `blocked` in a row
  (rate-limit), it stops early with a clear message — re-run the same command
  later to resume.
- **Robust.** One bad video never sinks the run; it becomes a record with the
  appropriate status and the batch continues.

### `status` values

| `status` | Meaning |
|----------|---------|
| `ok` | Transcript fetched. |
| `no_transcript` | The video has no subtitle track (or it's empty). |
| `transcript_disabled` | Captions are disabled by the uploader. |
| `blocked` | YouTube blocked the request (often datacenter-IP / rate limiting). |
| `video_unavailable` | Private, removed, age-restricted, or unplayable. |
| `invalid_input` | Could not extract a video id from the input. |
| `error` | Anything else; see the `message` field. |
| `skipped` | *(batch only)* Not attempted because the run stopped early on rate-limits — resume to fetch it. |

## Troubleshooting

- **`blocked` / "request blocked":** YouTube rate-limits requests from shared or
  datacenter IPs. Retry later, or use the manual-paste fallback.
- **`no_transcript` / `transcript_disabled`:** the video genuinely has no captions —
  the skill won't guess its content. Paste a transcript if you have one.
- **Toolchain provisioning fails:** check network access to `github.com` and
  `nodejs.org`, and that `curl`/`tar` are installed (see
  [Requirements](#requirements)). Re-running the command resumes provisioning —
  it only downloads what is still missing.

## Privacy & security

Everything runs locally on your machine. The script makes outbound requests only to
YouTube (for metadata and subtitles). Your browser's YouTube cookies are read
locally to authenticate those requests and are sent nowhere else; fetched
transcripts are cached under `.cache/` (git-ignored).

## Project structure

```
tldw/
├── SKILL.md            # skill manifest: triggers, workflow, formats, status→behavior
├── scripts/
│   ├── transcript.py   # URL/id parsing, list/fetch/batch, status codes, caching, manifest
│   └── transcript.sh   # self-provisioning bootstrap: downloads .runtime/, execs the fetcher
├── .runtime/           # yt-dlp standalone + bgutil PO-token provider + Node (git-ignored)
├── README.md
└── LICENSE
```

## Contributing

Issues and pull requests are welcome. The fetcher is intentionally small — keep
summarization logic out of it; that belongs to Claude. When updating the yt-dlp
binary in `.runtime/`, re-check `_classify_stderr` in `scripts/transcript.py`
against yt-dlp's current error strings.

## License

[MIT](LICENSE).

> This skill reads publicly available YouTube subtitles. Respect YouTube's Terms of
> Service and the rights of content creators when using it.
