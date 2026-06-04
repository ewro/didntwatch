---
name: didntwatch
description: |
  Summarize a YouTube video from its transcript/subtitles — "too long; didn't
  watch". The user gives a YouTube link (any format) or a bare video id and gets
  a summary without opening the video; the transcript then stays available for
  follow-up questions, quotes, and re-formatting.

  ACTIVATE when the user shares a YouTube link or video id and asks to summarize,
  recap, extract key points, get timecodes, or otherwise understand a video
  without watching it. Especially on phrases like:
    - "что в этом видео", "сделай выжимку / краткое содержание / тезисы"
    - "перескажи ролик", "о чём это видео", "didntwatch <ссылка>"
    - "summarize this video", "tldr/tldw/didntwatch this", "key points / takeaways"
    - "what does this video say", "recap with timestamps"
  Also activate when the user pastes a transcript text and asks to summarize it.

  ALSO ACTIVATE in BATCH mode when there is more than one video: several links at
  once, a file or browser-bookmarks folder of links, or a request to report on a
  list — "по каждому видео", "сделай отчёт по этим роликам", "выжимка по списку",
  "summarize each of these", "make a report from these links".

  SKIP for: non-YouTube videos with no transcript text, downloading the video
  itself, or audio/video transcription from a media file (this skill only reads
  existing YouTube subtitles or user-pasted text).
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Task
  - Bash(scripts/transcript.sh:*)
  - Bash(*/scripts/transcript.sh:*)
---

# didntwatch — too long; didn't watch

Turn a YouTube video into a summary from its **transcript** (subtitles). This
skill does one hard thing well: it reliably pulls a clean transcript into the
conversation. The "smart" parts — the summary, follow-up Q&A, quotes,
re-formatting, translation — are **you (Claude) working on that transcript**, not
script logic. A summary is just the default first action.

**Never invent content.** Everything in a summary must come from the transcript
(or the user's pasted text). If there is no transcript and none was pasted, say
so plainly — do not guess what the video is about.

## Voice — what the user sees (read this first)

Everything below this section is **your private operating manual**. The user must
never see the machinery. To them this skill is magic: they give a video, they get
an answer. They do not know — and must not be told — that there is a transcript, a
script, a cache, an index, a manifest, sub-agents, fetching, statuses, or
"hits/misses". **Hide all internal state.**

Hard rules for everything you say to the user:

- **Never name the mechanics.** Banned from user-facing text: *transcript,
  subtitles* (as the thing you read), *cache / cached / re-fetch / fetch, index,
  manifest, status, sub-agent, batch job, manifest, JSON*. Talk only about **the
  video** and **the summary**.
- **No progress narration.** Don't say "let me fetch the transcript", "retrieving
  from cache", "it's already cached", "running the script", "re-using what I
  pulled earlier". Just do the work silently and present the result. The tool
  calls themselves are enough; your prose should jump straight to the answer.
- **Recall sounds like memory, not lookup.** When the user drills into a video you
  covered before, never say "this is cached" or "I found it in the cache". Just
  answer about the video as if you simply remember it.
- **Failures are about the video, not the plumbing.** Phrase them in human terms:
  *"This video doesn't have captions, so there's nothing for me to read — if you
  paste the text, I'll summarize that."* / *"I can't open this video (it looks
  private or removed)."* / *"YouTube is rate-limiting right now — try again in a
  bit, or paste the text."* Do **not** expose status codes or say "fetch
  failed" / "blocked by datacenter IP" / "no transcript track".
- **The manual-paste fallback** is offered as *"paste the text and I'll summarize
  it"* — never *"paste the transcript"*.

In short: the user experiences a video summarizer with a perfect memory, never a
pipeline. Keep all the vocabulary below strictly on your side of the curtain.

## Two modes — pick one

These are two deliberately different modes with **opposite context contracts**:

- **Single mode (default; one video).** The full transcript lands **in your own
  context** (and on disk); you summarize it yourself and *stay available* for
  follow-up questions, quotes, section drill-downs, and re-formatting without
  re-fetching. Use the **Workflow** below.
- **Batch mode (a list of videos).** You **deliberately do not read transcripts
  into your context** — they would not fit for ~100 videos. The script fetches
  them into the cache and hands back a light manifest; summaries are produced
  over the cache (ideally by sub-agents, see **Batch workflow**). The trade-off:
  you can't deep-dive a single video from the batch afterwards *in the same
  breath* — but the bridge below recovers that.

**How to choose.** One link / "tell me about this video" / a conversation is
expected → single. Several links / a file or bookmarks folder / "for each" /
"make a report" → batch. **If unsure, ask the user which they want.**

**Bridge between modes (via the cache).** Batch still writes every transcript to
`.cache`. So if, after a report, the user wants to drill into one specific video,
that's just single mode for one id: locate it with `find`, load it back with
`get`, and have the normal conversation. The modes dock through the cache;
they don't conflict. See **Recall** below — do **not** re-fetch from YouTube or
hunt through agent logs for something already cached.

## Install / run

The helper script self-provisions its fetching toolchain on first run (yt-dlp
standalone + bgutil PO-token provider, plus a local Node runtime only if the
system lacks `node` ≥ 18). No manual setup needed — expect a one-time download
on the very first call.

```bash
# from the skill folder
scripts/transcript.sh list  "<youtube-url-or-id>"          # what tracks exist
scripts/transcript.sh fetch "<youtube-url-or-id>"          # best real track (manual > original auto)
scripts/transcript.sh fetch "<youtube-url-or-id>" --lang ru # prefer a specific REAL track (rare)

# viewer comments (top threads + uploader replies/hearts; no API key):
scripts/transcript.sh comments "<youtube-url-or-id>" [--max 100] [--sort top|new]

# batch mode (many videos -> cache + manifest, slim JSONL on stdout):
scripts/transcript.sh batch --input urls.txt --manifest manifest.json [--lang ru]
scripts/transcript.sh batch "<url1>" "<url2>" ...          # ids/urls as args too

# recall from cache (offline — no network, no runtime needed):
scripts/transcript.sh find "<id|url|title words|topic>"    # locate a CACHED transcript
scripts/transcript.sh get  "<url-or-id>" [--lang ru]       # print a CACHED transcript
scripts/transcript.sh reindex                              # rebuild the lookup index
```

Accepts every common link form (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`,
`/live/`, extra params like `&t=`) and a bare 11-char id.

## Workflow

1. **Get the transcript.** Run `scripts/transcript.sh fetch "<input>"` — no
   `--lang`. It returns the video's best real track (author-uploaded subtitles
   if any, else the original auto captions), prints JSON and caches it to
   `.cache/<id>.<lang>.json`. The **summary language is your job**, not the
   transcript's: you summarize an English transcript in Russian (or any
   requested language) directly — never ask YouTube for a translated track.
   `--lang <code>` exists only to pick among several real tracks (e.g. a video
   whose author uploaded subtitles in multiple languages); when no such track
   exists the result carries `requested_lang`/`lang_fallback: true` — just
   proceed with what came back.
2. **Branch on `status`** (see table below).
3. **On `ok`, summarize** in the chosen format/length/language using only
   `text` / `segments`. Then stay available: the transcript is in context (and
   on disk) — answer follow-up questions, pull quotes, drill into sections, or
   switch format without re-fetching.
4. **Offer commentary (single mode only).** If the video has anything worth
   appraising (claims, an argument, advice — not pure entertainment), close with
   **one** line offering your own take, e.g. *"Хочешь, добавлю свой комментарий —
   насколько обоснованна и сбалансированна позиция автора?"* Do **not** write the
   commentary unless asked. See **Commentary** below.

A requested summary language ("summarize in Russian") does **not** change the
fetch — plain `fetch`, then write the summary in that language yourself. Use
`list` when you need to show or choose among available tracks.

## Recall — drilling into an already-seen video

When the user asks for **more detail about one specific video they've seen
before** — "tell me more about video 38", "what did the parenting one say
exactly", a follow-up days after a batch report — the transcript is almost
certainly already in `.cache`. **Retrieve it from the cache; do not re-fetch from
YouTube and never grep through `~/.claude` agent logs.**

1. **Locate it with `find`** (offline, fuzzy). The query can be a video id, a
   URL, words from the title, or a **topic that only appears in the body** — it
   first matches titles/authors, then falls back to scanning transcript text and
   ranks by how many distinct query words a video covers (`coverage`), returning
   a `snippet` so you can confirm the right hit:
   ```bash
   scripts/transcript.sh find "родительское поведение страхи"   # -> ranked cache hits
   ```
   If you already know the number from a batch report, that report *is* a
   number→URL index: read the URL for that entry and skip straight to step 2.
2. **Load the transcript via the script — not the Read tool.** Run
   `scripts/transcript.sh get <id>` (add `--lang` if needed) to print the text,
   then answer from it (same no-fabrication rule). **Prefer `get` over `Read`ing
   the `cache_file` directly:** the script is the single allow-listed entry point
   (see *Permissions* below), so going through it means no permission prompt;
   opening the cache file with the Read tool can trigger one. Only fall back to
   `Read cache_file` if `get` can't give you what you need.
3. **Only if `find` returns `not_cached`** (the video was never cached, or the
   cache was cleared) do you `fetch` it. `fetch`/`batch` keep the index in step
   automatically; run `reindex` once if the cache predates the index or looks
   stale.

The index lives at `.cache/index.json` (id.lang → title/author/cache_file) and is
maintained on every successful fetch, so `find` stays cheap and offline.

## Comments — community reaction (on request)

When the user asks what viewers think — "что в комментариях", "как отреагировали",
"what do the comments say", "did anyone push back" — fetch them:

```bash
scripts/transcript.sh comments "<url-or-id>"            # top ~100, replies included
scripts/transcript.sh comments "<url-or-id>" --sort new --max 50
```

The record carries `comment_count` (the video's total) next to `fetched_count`
(what you actually got). Frame your analysis honestly — "по топ-15 из 66
комментариев", never as if you read them all. A `fetch` record also exposes
`comment_count`, so after a summary you can offer comment analysis knowing
whether there's anything to analyze.

What to surface, in order of value:

1. **Corrections and pushback** — commenters disputing or fixing the video's
   claims; these often outdate the content itself (patches, new data, errata).
2. **The uploader's own activity** — replies (`author_is_uploader`), hearted
   (`is_favorited`) and pinned (`is_pinned`) comments: what the author
   confirmed, walked back, or highlighted.
3. **Dominant themes and sentiment** — what most comments are actually about.
4. **Good unanswered questions** — often the video's blind spots.

Comments are viewer opinions, not facts: attribute them ("commenters report…",
"several viewers dispute…") and never blend them into the video's summary as if
the author said it. In commentary mode they count as evidence of *reception*,
not of truth. Same no-fabrication rule — only what's in the fetched comments.
Single mode / recall only — never in batch. The same Voice rules apply: the
user hears "judging by the comments…", never the machinery that got them.

## Output formats (user picks; **default = key theses**)

- **Short summary** — a few sentences capturing the gist.
- **Key theses** — a bulleted list of the main points *(default)*.
- **Sectioned recap with timecodes** — group `segments` into topical sections;
  for each, show `mm:ss` and a deep link `https://youtu.be/<id>?t=<start_seconds>`
  built from the segment's `start`.

The user may also set the summary **language** (default: the language they wrote
in, else the transcript language) and **length** (default: medium). Honor both.

## Commentary — Claude's own take (on request only)

The summary answers *what the video says*. Commentary answers *what you think of
it*: is the argument sound, balanced, current, does it hold up against outside
knowledge. This is the **opposite contract** to the summary — here you may and
should use knowledge beyond the transcript. Keep the two layers strictly
separate: the summary stays transcript-only; commentary is clearly your own.

**Never auto-generate it. It runs in exactly two cases:**

- The user explicitly asks — "разбери критически", "насколько это обоснованно",
  "сравни с консенсусом", "что думаешь", "is this legit", etc.
- Otherwise, **after a standard single-video summary, you offer it once** (see
  Workflow step 4) and write it only if the user says yes.

**Single mode / recall only — never in batch** (a web lookup per video × N is too
costly and slow).

How to write it:

- **Label it, once.** Open the block with a header **"Комментарий от Claude"** (in
  the user's language — e.g. "Claude's take"). That label *is* the signal that
  this is judgment, not gospel — so do **not** keep sprinkling "это лишь моё
  мнение / just my opinion" disclaimers. Say it once via the header, then give the
  substance straight.
- **Separate claim from verdict.** State what the author argues, then whether it
  holds. Point-by-point verdict markers ✅ / ⚠️ / ❌ work well.
- **Ground it.** For checkable factual claims, compare against established
  consensus and cite sources; use web search when stakes or recency warrant
  (fast-moving science, news, prices). Distinguish "the author's framing" from
  "where the evidence actually is". Decide this **per load-bearing claim, not per
  video** — verify only the claims the verdict rests on, and back each one you
  verify with a source.
- **Calibrate, and show direction of update.** State confidence plainly; if you
  re-check against fresh evidence, say which way your view moved and by how much.
  Don't hedge into mush.
- **Domain-risk caveats are the exception — spell those out explicitly.** For
  medicine, law, finance, safety and similar high-stakes domains, add a distinct,
  visible note that this is not authoritative and point to primary or professional
  sources. This is the *one* place to add an explicit caveat; everywhere else the
  label already does the job.
- **Skip when there's nothing to appraise** — pure entertainment, vlogs, music.
  Don't offer or write commentary on content with no checkable claims or argument.

## `status` → what to do

| `status`              | Meaning                                  | Your response |
|-----------------------|------------------------------------------|---------------|
| `ok`                  | Transcript fetched                       | Summarize in the chosen format; offer other formats/follow-ups. If `available_tracks` lists more languages, mention the user can switch. |
| `no_transcript`       | No subtitle track (or empty)             | Say plainly the video has no subtitles. Do **not** invent content. Offer the manual-paste fallback. |
| `transcript_disabled` | Captions disabled by the uploader        | Same as above — explain captions are turned off, offer manual paste. |
| `blocked`             | YouTube blocked the request / rate limit | Explain access was blocked (often datacenter-IP/rate limiting), not the user's fault. Offer manual paste; optionally suggest retrying later. |
| `video_unavailable`   | Private/removed/age-restricted/unplayable| Explain the video can't be accessed. Offer manual paste if they have the text. |
| `invalid_input`       | Couldn't parse a video id                | Ask for a valid YouTube link or 11-char id. |
| `error`               | Anything else (see `message`)            | Relay the cause briefly; offer manual paste. |

## Manual-paste fallback

Whenever fetching fails (`no_transcript`, `transcript_disabled`, `blocked`,
`video_unavailable`, `error`), offer it in plain words — *"paste the text of the
video and I'll summarize that instead"* (never "paste the transcript"; see
*Voice*). When the user pastes text, **skip the script entirely** and summarize
the pasted text directly, in the requested format/length/language — same
no-fabrication rule applies.

## Examples

- *"didntwatch https://youtu.be/dQw4w9WgXcQ"* → `fetch`, then key theses (default).
- *"Перескажи это видео по разделам с тайм-кодами: <ссылка>"* → `fetch`, then
  sectioned recap with `?t=` deep links, in Russian.
- *"Summarize in English, short: <shorts-url>"* → `fetch` (no `--lang`), then
  2–3 sentences in English.
- *"No captions? Here's the transcript: <pasted text>"* → summarize the pasted
  text, no fetch.
- After a summary: *"What did they say about pricing?"* → answer from the cached
  transcript, no re-fetch.

## Batch workflow (a list of videos → one report)

Goal: a markdown report with a short summary per video, **without ever loading
all transcripts into context**. The script does the one hard thing — fetching —
politely and in one pass; you build the report over the cache.

**Step 1 — collect the targets.** Gather the links from the message, a file, or
by **extracting them yourself** from browser bookmarks / any HTML or text the
user gives (the script does *not* parse bookmarks — that part is on you). Write
one URL/id per line into `urls.txt` (blank lines and `#` comments are ignored).

**Step 2 — fetch in one throttled pass.**
```bash
scripts/transcript.sh batch --input urls.txt --manifest manifest.json [--lang ru]
```
This is the *only* place that touches the network, and it's deliberately polite
(`--concurrency 2`, `--delay 1.0`, exponential backoff on blocks). It streams a
slim record per video to stdout and writes the full array to `manifest.json`;
**full transcripts go only into `.cache`, never stdout**. You do not need to read
stdout — read `manifest.json`. It is **resumable**: re-running the same command
skips videos already cached and only fetches what's missing (`--refresh` forces a
re-fetch). If it **stops early** with a rate-limit message, tell the user access
was throttled and offer to resume later — the same command will pick up where it
left off.

**Step 3 — build the report from the cache (do NOT pour transcripts into your
own context).** Reading cache files one-by-one in the main context does not solve
the problem: each transcript lingers in history and context still grows linearly.
The real off-load is a **sub-agent**, whose context is destroyed on return.

- **Preferred — fan-out via sub-agents (map-reduce).** Take the `ok` records from
  `manifest.json`, split them into chunks of ~10–15, and spawn one sub-agent per
  chunk (≈9 agents for 100 videos — chunk, don't do 1 agent per video; spawns
  aren't free). Give each sub-agent: the list of video **ids** for **its chunk
  only**, the desired summary format/length/language, and an explicit number range
  `N..M` so numbering doesn't collide. Each sub-agent loads each video with
  `scripts/transcript.sh get <id>` (no network, no prompt — see *Permissions*),
  writes summaries, and **returns only the finished numbered markdown blocks**
  (never the raw text — that's the whole point). You stitch the blocks back
  together in order.
- **Fallback — no sub-agents.** Walk the manifest in chunks of 10–20; for each
  `ok` record load it with `scripts/transcript.sh get <id>`, write the summary,
  and **append it to the output file** (Edit/Write) between chunks rather than
  holding texts in context. Know that context still grows somewhat, so compaction
  may happen near the end on large lists.

**Step 4 — the videos that failed.** Collect every non-`ok` record
(`no_transcript`, `transcript_disabled`, `blocked`, `video_unavailable`, `error`,
`invalid_input`, `skipped`) into a separate **"Couldn't process"** section with
the title, link, and reason. Do **not** invent what those videos were about.

### Default report format (user can override)

Markdown, numbered list. For each video:

```markdown
**N. <Title>**
https://youtu.be/<id>
<2–4 sentence summary>
```

Summary language = the **user's request language** (default), independent of the
subtitle language. End with the "Couldn't process" section. Write to the path the
user gave (e.g. `./yt/2026-spring.md`); create the folder if needed. Same
no-fabrication rule as single mode: every summary rests on a real transcript.

### Batch example

*"Here's my bookmarks file — pull every YouTube link from folder X and give a
short summary of each, save to `./yt/2026-spring.md` with numbering and links."*
→ extract the links yourself into `urls.txt` → `batch --input urls.txt --manifest
manifest.json` → fan out sub-agents over the `ok` chunks → stitch the numbered
report + a "Couldn't process" section.

## Permissions — keeping it prompt-free

The user should never get pinged for permission while you operate. The trick is
**one entry point, one allow rule**:

- **Do everything through `scripts/transcript.sh`.** `fetch`, `find`, `get`,
  `batch`, `list`, `reindex` all print what you need to stdout, so you almost
  never need the `Read` tool on internal files. The frontmatter already
  allow-lists this script (`Bash(scripts/transcript.sh:*)` and
  `Bash(*/scripts/transcript.sh:*)`), so those calls run without a prompt.
- **Avoid the `Read` tool on `.cache/*` and `.runtime/*`.** Those live inside the
  skill folder (`~/.claude/skills/didntwatch/`), which is outside the user's project
  working directory, so opening them with `Read` is what triggers a prompt. Use
  `get` instead — same data, no prompt. (Moving the cache elsewhere does **not**
  help: there is no folder that is universally "no-permission"; the `Read` prompt
  depends on the session's working directories, which differ per project. The fix
  is routing through the allow-listed script, not relocating files.)
- **Don't `cd` into the skill folder.** Call the script by the path the skill
  gives you (`scripts/transcript.sh ...`); a `cd ~/.claude/... && ...` compound
  command reads as a different, non-allow-listed command and can prompt.

Net effect: approve the script once (or rely on the frontmatter allow rule) and
the whole flow — summarize, recall, batch — runs silently.

## Installing the skill

This folder *is* the skill. Claude Code discovers skills in `~/.claude/skills/`,
so clone (or symlink) it there:

```bash
git clone https://github.com/<you>/didntwatch.git ~/.claude/skills/didntwatch
# or, to develop elsewhere and symlink:
ln -s /path/to/didntwatch ~/.claude/skills/didntwatch
```

The Python environment self-bootstraps on first run. See `README.md` for full docs.
