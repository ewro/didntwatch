#!/usr/bin/env python3
"""tldw transcript fetcher (yt-dlp + bgutil PO-token backend).

A deliberately small, single-purpose tool: it turns a YouTube URL (or bare
video ID) into a clean transcript and prints structured JSON. It does NOT
summarize — summarizing, Q&A and re-formatting are done by Claude on top of
the transcript this script returns.

Modes:
  list  <url|id>              -> available subtitle tracks
  fetch <url|id> [--lang xx]  -> one transcript (text + timestamped segments)
  batch [targets...]          -> many transcripts into the cache + a manifest

`fetch`/`list` print a single JSON object to stdout. `batch` prints one slim
JSON object per line (JSONL) and keeps the heavy transcript only in the cache.
Every record carries a top-level "status" field so the caller can branch
deterministically:
  ok | no_transcript | transcript_disabled | blocked |
  video_unavailable | invalid_input | error

Why yt-dlp + bgutil: YouTube now requires a Proof-of-Origin (PO) token for
subtitle downloads. We get past it with a self-contained toolchain under
`.runtime/` (provisioned by transcript.sh): the yt-dlp standalone binary
(bundles curl_cffi for browser TLS impersonation), a local Node runtime, and
the bgutil PO-token provider in script mode. Browser cookies (your YouTube
login) are the final key — pass them with --cookies-from-browser / --cookies
or the TLDW_COOKIES_FROM_BROWSER / TLDW_COOKIES_FILE env vars.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_DIR / ".cache"
# Lightweight, greppable map id.lang -> {title, author, cache_file, ...} so a
# transcript can be found by title/topic/url without opening every big JSON.
INDEX_FILE = CACHE_DIR / "index.json"
RUNTIME_DIR = SKILL_DIR / ".runtime"
YTDLP_BIN = RUNTIME_DIR / "bin" / "yt-dlp"
NODE_BIN_DIR = RUNTIME_DIR / "node" / "bin"
BGUTIL_PLUGIN_DIR = RUNTIME_DIR / "bgutil" / "plugin"
BGUTIL_SCRIPT = RUNTIME_DIR / "bgutil" / "server" / "build" / "generate_once.js"

# An 11-char YouTube video id: letters, digits, dash, underscore.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Same, but anywhere we can pull it out of a URL path/query.
_ID_IN_TEXT_RE = re.compile(r"([A-Za-z0-9_-]{11})")


def extract_video_id(raw: str) -> str | None:
    """Pull an 11-char video id out of any common YouTube URL form or a bare id.

    Handles: watch?v=, youtu.be/, /shorts/, /embed/, /live/, /v/, the
    ?v= / &v= query params, extra params (&t=, list=, si=), and a bare id.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    if _ID_RE.match(raw):
        return raw

    candidate = raw if "//" in raw else "https://" + raw
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.netloc or "").lower().lstrip("www.")
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    if "v" in query and query["v"]:
        cand = query["v"][0]
        if _ID_RE.match(cand):
            return cand

    if host.endswith("youtu.be"):
        seg = path.strip("/").split("/", 1)[0]
        if _ID_RE.match(seg):
            return seg

    parts = [p for p in path.split("/") if p]
    for i, seg in enumerate(parts):
        if seg in ("shorts", "embed", "live", "v") and i + 1 < len(parts):
            cand = parts[i + 1]
            if _ID_RE.match(cand):
                return cand

    for seg in parts:
        m = _ID_IN_TEXT_RE.match(seg)
        if m:
            return m.group(1)

    return None


# --- transcript index --------------------------------------------------------
# A small index so a cached transcript is findable by title / author / topic /
# url, not just by knowing its 11-char id. Keyed by "<id>.<lang>". It is
# maintained on every successful fetch and can be rebuilt with `reindex`.

_INDEX_FIELDS = (
    "video_id",
    "url",
    "title",
    "author",
    "language_code",
    "is_generated",
    "segment_count",
    "cache_file",
)


def _index_key(video_id: str, lang_code: str) -> str:
    return f"{video_id}.{lang_code}"


def _load_index() -> dict[str, dict]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(index: dict[str, dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _index_entry(record: dict) -> dict:
    entry = {k: record.get(k) for k in _INDEX_FIELDS}
    entry["fetched_at"] = record.get("fetched_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return entry


def _upsert_index(record: dict) -> None:
    """Add/update one cached transcript in the index. Best-effort, never raises."""
    vid, lang = record.get("video_id"), record.get("language_code")
    if not vid or not lang:
        return
    index = _load_index()
    index[_index_key(vid, lang)] = _index_entry(record)
    _save_index(index)


def cmd_reindex() -> dict:
    """Rebuild index.json by scanning every cached transcript JSON."""
    index: dict[str, dict] = {}
    rebuilt = 0
    for path in sorted(CACHE_DIR.glob("*.json")):
        if path.name == INDEX_FILE.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") != "ok" or not data.get("video_id"):
            continue
        data["cache_file"] = str(path)
        vid = data["video_id"]
        lang = data.get("language_code") or path.name.split(".")[-2]
        data.setdefault("language_code", lang)
        index[_index_key(vid, lang)] = _index_entry(data)
        rebuilt += 1
    _save_index(index)
    return {"status": "ok", "indexed": rebuilt, "index_file": str(INDEX_FILE)}


def _score_match(query: str, entry: dict) -> int:
    """Crude relevance: how many query tokens hit the title/author (substring)."""
    hay = " ".join(str(entry.get(k) or "") for k in ("title", "author", "video_id")).casefold()
    tokens = [t for t in query.casefold().split() if t]
    if not tokens:
        return 0
    score = sum(1 for t in tokens if t in hay)
    if query.casefold() in hay:  # full-phrase bonus
        score += len(tokens)
    return score


def cmd_find(query: str, lang: str | None, limit: int) -> dict:
    """Locate cached transcripts by id/url or by title/author/topic fragment.

    Resolution order: if the query is (or contains) a video id, return its
    cached track(s) directly; otherwise rank index entries by how many query
    tokens appear in the title/author. Returns slim records with `cache_file`
    so the caller can Read the transcript without any network call.
    """
    index = _load_index()
    if not index:  # self-heal: build it from whatever is cached
        cmd_reindex()
        index = _load_index()

    vid = extract_video_id(query)
    if vid:
        hits = [
            e for k, e in index.items()
            if e.get("video_id") == vid and (not lang or e.get("language_code") == lang)
        ]
        if hits:
            return {"status": "ok", "query": query, "matched_by": "id", "results": hits}
        # known id but not cached yet — tell the caller how to get it
        return {
            "status": "not_cached",
            "query": query,
            "video_id": vid,
            "message": f"No cached transcript for {vid}. Run: fetch {vid}"
            + (f" --lang {lang}" if lang else ""),
        }

    scored = []
    for entry in index.values():
        if lang and entry.get("language_code") != lang:
            continue
        s = _score_match(query, entry)
        if s > 0:
            scored.append((s, entry))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("title") or "")))
    results = [e for _, e in scored[: max(1, limit)]]
    if results:
        return {"status": "ok", "query": query, "matched_by": "title", "results": results}

    # Fallback: the topic may live in the transcript body, not the title
    # (e.g. "родительское поведение" inside a video titled about motivation).
    # Scan the cached transcript text — bounded to the cache, still no network.
    deep = _content_search(query, index, lang, limit)
    return {
        "status": "ok" if deep else "no_match",
        "query": query,
        "matched_by": "content",
        "results": deep,
        "message": None if deep else "No cached transcript matched. Try `reindex`, or `fetch` the video.",
    }


def _content_search(query: str, index: dict[str, dict], lang: str | None, limit: int) -> list[dict]:
    """Rank cached transcripts by how often the query terms appear in the body.

    Returns slim index entries augmented with a short `snippet` around the first
    hit, so the caller can confirm the match before reading the whole file.
    """
    tokens = [t for t in query.casefold().split() if t]
    if not tokens:
        return []
    phrase = query.casefold()
    distinct = set(tokens)
    scored: list[tuple[tuple, dict]] = []
    for entry in index.values():
        if lang and entry.get("language_code") != lang:
            continue
        cache_file = entry.get("cache_file")
        if not cache_file:
            continue
        try:
            data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = (data.get("text") or "").casefold()
        if not text:
            continue
        # Coverage (how many *distinct* query words appear) dominates raw
        # frequency, so one common word repeated 15× can't outrank a video
        # that actually contains most of the query.
        coverage = sum(1 for t in distinct if t in text)
        if coverage == 0:
            continue
        hits = sum(text.count(t) for t in distinct)
        has_phrase = 1 if phrase in text else 0
        pos = text.find(phrase) if has_phrase else next(
            (text.find(t) for t in tokens if text.find(t) >= 0), -1
        )
        snippet = ""
        if pos >= 0:
            raw = data.get("text") or ""
            snippet = raw[max(0, pos - 60): pos + 140].replace("\n", " ").strip()
        out = dict(entry)
        out["coverage"] = f"{coverage}/{len(distinct)}"
        out["match_count"] = hits
        out["snippet"] = snippet
        scored.append(((coverage, has_phrase, hits), out))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[: max(1, limit)]]


def cmd_get(video_id: str, lang: str | None, as_json: bool) -> dict:
    """Print a cached transcript without touching the network.

    Default returns the plain `text`; with `--json` the full cached record.
    Use this for drill-down after a batch instead of re-fetching.
    """
    record = _read_cache(video_id, lang)
    if record is None:
        return {
            "status": "not_cached",
            "video_id": video_id,
            "message": f"No cached transcript for {video_id}"
            + (f" in {lang}" if lang else "")
            + ". Run `fetch` to retrieve it.",
        }
    if as_json:
        return record
    return {
        "status": "ok",
        "video_id": video_id,
        "language_code": record.get("language_code"),
        "title": record.get("title"),
        "cache_file": record.get("cache_file"),
        "text": record.get("text", ""),
    }


def _read_cache(video_id: str, lang: str | None) -> dict | None:
    """Load the full cached record for an id (+ optional lang) from disk."""
    if lang:
        candidates = [CACHE_DIR / f"{video_id}.{lang}.json"]
    else:
        candidates = sorted(CACHE_DIR.glob(f"{video_id}.*.json"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["cache_file"] = str(path)
        return data
    return None


# --- yt-dlp plumbing ---------------------------------------------------------

# Process-wide cookie/impersonate config, set once from CLI args + env in main().
_COOKIE_ARGS: list[str] = []
_IMPERSONATE = os.environ.get("TLDW_IMPERSONATE", "chrome")


def _resolve_cookie_args(cookies_from_browser: str | None, cookies_file: str | None) -> list[str]:
    """Build the yt-dlp cookie flags from CLI args, falling back to env vars.

    Precedence: explicit --cookies file > --cookies-from-browser > env file >
    env browser > default browser (firefox). Cookies are what get past the
    PO-token wall, so we always pass *something* unless explicitly disabled
    with TLDW_COOKIES_FROM_BROWSER=none.
    """
    if cookies_file:
        return ["--cookies", cookies_file]
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser]
    env_file = os.environ.get("TLDW_COOKIES_FILE")
    if env_file:
        return ["--cookies", env_file]
    env_browser = os.environ.get("TLDW_COOKIES_FROM_BROWSER", "firefox")
    if env_browser and env_browser.lower() != "none":
        return ["--cookies-from-browser", env_browser]
    return []


def _ytdlp_env() -> dict:
    """Environment for yt-dlp: prepend the bundled Node so bgutil can run."""
    env = os.environ.copy()
    if NODE_BIN_DIR.is_dir():
        env["PATH"] = f"{NODE_BIN_DIR}{os.pathsep}{env.get('PATH', '')}"
    return env


def _ytdlp_base() -> list[str]:
    """Common yt-dlp args: PO-token plugin, impersonation, cookies."""
    args = [str(YTDLP_BIN)]
    if BGUTIL_PLUGIN_DIR.is_dir() and BGUTIL_SCRIPT.is_file():
        args += [
            "--plugin-dirs",
            str(BGUTIL_PLUGIN_DIR),
            "--extractor-args",
            f"youtubepot-bgutilscript:script_path={BGUTIL_SCRIPT}",
        ]
    if _IMPERSONATE and _IMPERSONATE.lower() != "none":
        args += ["--impersonate", _IMPERSONATE]
    args += _COOKIE_ARGS
    return args


def _run_ytdlp(extra: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Run yt-dlp with the common args + `extra`. Never raises on non-zero."""
    cmd = _ytdlp_base() + extra
    return subprocess.run(
        cmd,
        env=_ytdlp_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _classify_stderr(stderr: str) -> tuple[str, str]:
    """Map yt-dlp stderr onto a (status, message). Best-effort heuristics."""
    s = (stderr or "").lower()
    if "http error 429" in s or "too many requests" in s:
        return ("blocked", "YouTube rate-limited the request (HTTP 429).")
    if "sign in to confirm" in s or "not a bot" in s:
        return ("blocked", "YouTube demanded sign-in/bot confirmation (cookies/PO token issue).")
    if "po token" in s and "subtitle" in s:
        return ("blocked", "Subtitles require a PO token that could not be generated.")
    if "private video" in s or "members-only" in s or "join this channel" in s:
        return ("video_unavailable", "Video is private or members-only.")
    if "video unavailable" in s or "is not available" in s or "has been removed" in s:
        return ("video_unavailable", "Video is unavailable (removed/region-locked).")
    if "age" in s and "restrict" in s:
        return ("video_unavailable", "Video is age-restricted.")
    if "subtitles" in s and "disabled" in s:
        return ("transcript_disabled", "Subtitles are disabled for this video.")
    return ("error", (stderr or "").strip().splitlines()[-1] if stderr.strip() else "yt-dlp failed.")


# --- subtitle parsing --------------------------------------------------------


def _ts_to_seconds(ts: str) -> float:
    """'00:01:02.500' or '00:01:02,500' -> 62.5"""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(parts[0])
    except ValueError:
        return 0.0


def _parse_json3(text: str) -> list[dict]:
    """Parse YouTube's json3 caption format into clean segments.

    json3 has discrete events with no rolling-window duplication, so it yields
    far cleaner text than auto-caption VTT. Each event: tStartMs + segs[].utf8.
    """
    data = json.loads(text)
    out: list[dict] = []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        line = "".join(seg.get("utf8", "") for seg in segs).strip()
        if not line:
            continue
        start = round(ev.get("tStartMs", 0) / 1000.0, 3)
        dur = round(ev.get("dDurationMs", 0) / 1000.0, 3)
        out.append({"start": start, "dur": dur, "text": line})
    return out


_VTT_CUE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def _parse_vtt(text: str) -> list[dict]:
    """Parse a WebVTT/SRT file into segments, de-duplicating rolling captions.

    Auto-caption VTT repeats the previous line plus a new word in each cue; we
    strip inline timing tags and drop a cue whose text is identical to, or a
    leading subset of, what we already kept.
    """
    lines = text.splitlines()
    raw: list[tuple[float, str]] = []
    i = 0
    while i < len(lines):
        m = _VTT_CUE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _ts_to_seconds(m.group(1))
        end = _ts_to_seconds(m.group(2))
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip() and not _VTT_CUE_RE.search(lines[i]):
            buf.append(_VTT_TAG_RE.sub("", lines[i]).strip())
            i += 1
        cue = " ".join(x for x in buf if x).strip()
        if cue:
            raw.append((start, end, cue))
    # De-dup rolling captions.
    out: list[dict] = []
    last_text = ""
    for start, end, cue in raw:
        if cue == last_text:
            continue
        # rolling window: previous text is a prefix of the current cue
        if last_text and cue.startswith(last_text):
            out[-1] = {"start": out[-1]["start"], "dur": round(end - out[-1]["start"], 3), "text": cue}
            last_text = cue
            continue
        out.append({"start": round(start, 3), "dur": round(end - start, 3), "text": cue})
        last_text = cue
    return out


def _parse_sub_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json3" or (text.lstrip().startswith("{") and '"events"' in text[:200]):
        try:
            return _parse_json3(text)
        except (ValueError, KeyError):
            pass
    return _parse_vtt(text)


# --- core fetch --------------------------------------------------------------


# The single original auto-caption track always carries a "-orig" suffix; the
# ~150 machine-translation targets never do, so this selector cannot touch
# YouTube's per-language translation endpoints (which aggressively 429).
_ORIG_SELECTOR = ".*-orig"


def _base_lang(code: str | None) -> str | None:
    """'en-orig' / 'pt-BR' -> 'en' / 'pt' for loose language comparison."""
    if not code:
        return None
    return code.removesuffix("-orig").split("-")[0].lower()


def _download_sub(
    video_id: str, selector: str, outdir: Path, *, manual_only: bool = False
) -> tuple[list[dict] | None, dict, str]:
    """Download the subtitle track matching `selector` into `outdir`.

    `selector` is a yt-dlp --sub-langs pattern: an exact code ("ru") or a
    regex (".*-orig"). With `manual_only`, only author-uploaded subtitles are
    requested — they live in a separate namespace from auto captions, so a
    plain code can never resolve to a machine translation.
    Returns (segments|None, info, stderr). `info` is the parsed info.json
    (title/uploader/available tracks) or {} on failure.
    """
    extra = [
        "--skip-download",
        "--ignore-no-formats-error",
        "--no-warnings",
        "--write-subs",
        "--write-info-json",
        "--sub-langs",
        selector,
        "--sub-format",
        "json3/vtt/best",
        "--retries",
        "5",
        "--retry-sleep",
        "8",
        "-o",
        str(outdir / "%(id)s.%(ext)s"),
        f"https://youtu.be/{video_id}",
    ]
    if not manual_only:
        extra.insert(4, "--write-auto-subs")
    proc = _run_ytdlp(extra)
    info: dict = {}
    info_path = outdir / f"{video_id}.info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except ValueError:
            info = {}
    sub_files = sorted(
        p for p in outdir.glob(f"{video_id}.*") if p.suffix in (".json3", ".vtt", ".srt")
    )
    if sub_files:
        segs = _parse_sub_file(sub_files[0])
        if segs:
            return segs, info, proc.stderr
    return None, info, proc.stderr


def _available_tracks(info: dict) -> list[dict]:
    """Build an available_tracks list from an info.json dict.

    YouTube's automatic_captions lists the source auto-caption *plus* a machine
    translation into every supported language (~150 entries). We keep only the
    genuine tracks — all manual subtitles, and the auto-caption(s) that are the
    video's own language (its code, an `*-orig` variant) — and drop the
    translation targets, which would otherwise bloat the cache and manifest.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    video_lang = (info.get("language") or "").lower()

    tracks: list[dict] = []
    for code, items in manual.items():
        name = items[0].get("name") if items else None
        tracks.append({"language": name or code, "language_code": code, "is_generated": False})
    for code, items in auto.items():
        if code in manual:
            continue
        base = code.lower().split("-")[0]
        is_original = code.endswith("-orig") or code.lower() == video_lang or (
            video_lang and base == video_lang.split("-")[0]
        )
        if not is_original:
            continue
        name = items[0].get("name") if items else None
        tracks.append({"language": name or code, "language_code": code, "is_generated": True})
    return tracks


def _fetch_one(video_id: str, lang: str | None) -> dict:
    """Fetch the best genuine subtitle track and persist it. Returns a full record.

    Shared core behind both `fetch` and `batch`. Two-step strategy that never
    requests YouTube's machine translations:
      1. An optimistic call grabs the original auto-caption track (".*-orig")
         plus info.json — for most videos that is the whole job.
      2. If info.json shows author-uploaded subtitles (cleaner than ASR), or
         `lang` names one, a second exact call fetches that manual track.
    `lang` only chooses among the video's real tracks; when it matches none,
    the best real track is returned with `requested_lang`/`lang_fallback` set —
    summary-language translation is the caller's job, not YouTube's.
    """
    with tempfile.TemporaryDirectory(prefix="tldw-") as tmp:
        orig_dir = Path(tmp) / "orig"
        orig_dir.mkdir()
        orig_segs, info, last_stderr = _download_sub(video_id, _ORIG_SELECTOR, orig_dir)
        tracks = _available_tracks(info)
        manual_tracks = [t for t in tracks if not t["is_generated"]]
        orig_code = _detect_lang_from_files(orig_dir, video_id)

        # Pick the manual track worth a second call, if any.
        target: dict | None = None
        fallback = False
        if lang:
            target = next(
                (t for t in manual_tracks if _base_lang(t["language_code"]) == _base_lang(lang)),
                None,
            )
            if target is None and not (orig_segs and _base_lang(orig_code) == _base_lang(lang)):
                fallback = True  # no real track in the requested language
        if target is None and (not lang or fallback) and manual_tracks:
            # Prefer the manual track in the video's own language. info.json's
            # `language` is often absent (tv client), so the original
            # auto-caption track — the spoken language — is the fallback proxy;
            # last resorts: English, then whatever comes first.
            video_lang = _base_lang(info.get("language")) or _base_lang(
                orig_code
                or next((t["language_code"] for t in tracks if t["is_generated"]), None)
            )
            target = next(
                (t for t in manual_tracks if _base_lang(t["language_code"]) == video_lang),
                next(
                    (t for t in manual_tracks if _base_lang(t["language_code"]) == "en"),
                    manual_tracks[0],
                ),
            )

        if target is not None:
            man_dir = Path(tmp) / "manual"
            man_dir.mkdir()
            man_segs, man_info, man_stderr = _download_sub(
                video_id, target["language_code"], man_dir, manual_only=True
            )
            last_stderr = man_stderr or last_stderr
            if man_segs:
                code = _detect_lang_from_files(man_dir, video_id) or target["language_code"]
                return _build_ok(
                    video_id, info or man_info, code, man_segs,
                    is_generated=False, requested=lang, fallback=fallback,
                )
            # Manual fetch failed — the original track is still a good answer.

        if orig_segs:
            return _build_ok(
                video_id, info, orig_code or "und", orig_segs,
                is_generated=True, requested=lang, fallback=fallback,
            )

        # Nothing downloaded. info.json knowing no real tracks beats stderr
        # noise: a failed probe often *also* leaves an unrelated error line.
        if info and not tracks:
            return _fail(video_id, info, "no_transcript", "No subtitle tracks found for this video.")
        status, message = _classify_stderr(last_stderr)
        return _fail(video_id, info, status, message)


def _detect_lang_from_files(tmpdir: Path, video_id: str) -> str | None:
    for p in sorted(tmpdir.glob(f"{video_id}.*")):
        if p.suffix in (".json3", ".vtt", ".srt"):
            # filename is <id>.<lang>.<ext>
            parts = p.name.split(".")
            if len(parts) >= 3:
                return parts[-2]
    return None


def _build_ok(
    video_id: str,
    info: dict,
    track_code: str,
    segments: list[dict],
    *,
    is_generated: bool,
    requested: str | None = None,
    fallback: bool = False,
) -> dict:
    full_text = "\n".join(s["text"] for s in segments).strip()
    if not full_text:
        return _fail(video_id, info, "no_transcript", "Transcript track was empty.")
    # Cache/index under the plain language code ("en-orig" -> "en").
    result = {
        "status": "ok",
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": info.get("title"),
        "author": info.get("uploader"),
        "language_code": track_code.removesuffix("-orig"),
        "is_generated": is_generated,
        "available_tracks": _available_tracks(info),
        "segment_count": len(segments),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if requested and fallback:
        result["requested_lang"] = requested
        result["lang_fallback"] = True
    result["text"] = full_text
    result["segments"] = segments
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"{video_id}.{result['language_code']}.json"
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        result["cache_file"] = str(cache_file)
    except OSError:
        pass
    # Keep the lookup index in step with the cache.
    _upsert_index(result)
    return result


def _fail(video_id: str, info: dict, status: str, message: str) -> dict:
    return {
        "status": status,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": info.get("title"),
        "author": info.get("uploader"),
        "message": message,
    }


def cmd_fetch(video_id: str, lang: str | None) -> dict:
    return _fetch_one(video_id, lang)


def cmd_list(video_id: str) -> dict:
    """List available subtitle tracks via a metadata-only yt-dlp probe."""
    with tempfile.TemporaryDirectory(prefix="tldw-") as tmp:
        out = Path(tmp) / "%(id)s.%(ext)s"
        proc = _run_ytdlp(
            [
                "--skip-download",
                "--ignore-no-formats-error",
                "--no-warnings",
                "--write-info-json",
                "-o",
                str(out),
                f"https://youtu.be/{video_id}",
            ]
        )
        info_path = Path(tmp) / f"{video_id}.info.json"
        if info_path.is_file():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except ValueError:
                info = {}
            tracks = _available_tracks(info)
            return {
                "status": "ok" if tracks else "no_transcript",
                "video_id": video_id,
                "tracks": tracks,
            }
    status, message = _classify_stderr(proc.stderr)
    return {"status": status, "video_id": video_id, "message": message}


# --- batch mode --------------------------------------------------------------

_BLOCKED_STREAK_LIMIT = 5
_HEAVY_FIELDS = ("text", "segments")


def _slim(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in _HEAVY_FIELDS}


def _collect_targets(positional: list[str], input_file: str | None) -> tuple[list[str], list[dict]]:
    raw: list[str] = []
    if input_file:
        for line in Path(input_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw.append(line)
    raw.extend(positional)

    seen: set[str] = set()
    ids: list[str] = []
    invalid: list[dict] = []
    for target in raw:
        vid = extract_video_id(target)
        if not vid:
            invalid.append(
                {
                    "status": "invalid_input",
                    "video_id": None,
                    "input": target,
                    "message": "Could not extract a YouTube video id.",
                }
            )
            continue
        if vid in seen:
            continue
        seen.add(vid)
        ids.append(vid)
    return ids, invalid


def _cached_record(video_id: str, lang: str | None) -> dict | None:
    data = _read_cache(video_id, lang)
    return _slim(data) if data is not None else None


def _fetch_with_retry(video_id: str, lang: str | None, max_retries: int, base_delay: float) -> dict:
    last: dict = {}
    for attempt in range(max_retries + 1):
        try:
            last = _fetch_one(video_id, lang)
        except Exception as exc:  # noqa: BLE001 - one video must not crash batch
            last = {"status": "error", "video_id": video_id, "message": f"{type(exc).__name__}: {exc}"}
        if last.get("status") != "blocked" or attempt == max_retries:
            return last
        time.sleep(base_delay * (2 ** attempt) + random.uniform(0, base_delay))
    return last


def cmd_batch(args: argparse.Namespace) -> int:
    ids, invalid = _collect_targets(args.targets, args.input)
    total = len(ids)
    concurrency = max(1, args.concurrency)
    manifest: list[dict] = list(invalid)
    state = {"done": 0, "blocked_streak": 0, "stopped": False}

    def emit(record: dict, vid: str) -> None:
        manifest.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        state["done"] += 1
        status = record.get("status")
        print(f"tldw: {state['done']}/{total} {vid} {status}", file=sys.stderr)
        if status == "blocked":
            state["blocked_streak"] += 1
        elif status == "ok":
            state["blocked_streak"] = 0
        if state["blocked_streak"] >= _BLOCKED_STREAK_LIMIT and not state["stopped"]:
            state["stopped"] = True
            print(
                f"tldw: stopping early after {state['blocked_streak']} consecutive "
                "'blocked' results — YouTube is rate-limiting us (or cookies/PO token "
                "are not working). Re-run the same command later to resume.",
                file=sys.stderr,
            )

    for rec in invalid:
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        wave: list[str] = []

        def flush_wave() -> None:
            if not wave:
                return
            futures = []
            for vid in wave:
                futures.append(
                    (vid, pool.submit(_fetch_with_retry, vid, args.lang, args.max_retries, args.delay))
                )
                if args.delay:
                    time.sleep(args.delay)
            for vid, fut in futures:
                emit(_slim(fut.result()), vid)
            wave.clear()

        for vid in ids:
            if state["stopped"]:
                emit(
                    {
                        "status": "skipped",
                        "video_id": vid,
                        "url": f"https://youtu.be/{vid}",
                        "message": "Skipped after early stop (rate-limited).",
                    },
                    vid,
                )
                continue
            if not args.refresh:
                cached = _cached_record(vid, args.lang)
                if cached is not None:
                    emit(cached, vid)
                    continue
            wave.append(vid)
            if len(wave) >= concurrency:
                flush_wave()
        flush_wave()

    if args.manifest:
        Path(args.manifest).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


# --- CLI ---------------------------------------------------------------------


def _add_cookie_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--cookies-from-browser",
        default=None,
        help="browser to read YouTube cookies from (firefox, chrome, ...); "
        "defaults to $TLDW_COOKIES_FROM_BROWSER or 'firefox'",
    )
    p.add_argument(
        "--cookies", default=None, help="path to a cookies.txt file (overrides --cookies-from-browser)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="transcript", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list available subtitle tracks")
    p_list.add_argument("target", help="YouTube URL or 11-char video id")
    _add_cookie_args(p_list)

    p_fetch = sub.add_parser("fetch", help="fetch one transcript as JSON")
    p_fetch.add_argument("target", help="YouTube URL or 11-char video id")
    p_fetch.add_argument(
        "--lang",
        default=None,
        help="prefer this language among the video's REAL subtitle tracks "
        "(never machine translations); falls back to the best real track",
    )
    _add_cookie_args(p_fetch)

    p_batch = sub.add_parser(
        "batch", help="fetch many transcripts into the cache + write a manifest (JSONL out)"
    )
    p_batch.add_argument("targets", nargs="*", help="YouTube URLs or 11-char ids (with --input)")
    p_batch.add_argument("--input", default=None, help="file with one URL/id per line (# comments ok)")
    p_batch.add_argument(
        "--lang",
        default=None,
        help="preferred language among each video's real subtitle tracks "
        "(never machine translations)",
    )
    p_batch.add_argument("--manifest", default=None, help="write a JSON-array index of all records here")
    p_batch.add_argument("--concurrency", type=int, default=2, help="parallel requests (default: 2)")
    p_batch.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    p_batch.add_argument("--max-retries", type=int, default=3, help="backoff retries on block (default: 3)")
    p_batch.add_argument("--refresh", action="store_true", help="re-fetch even if a cache file exists")
    _add_cookie_args(p_batch)

    # Offline cache tools — no network, no runtime needed.
    p_find = sub.add_parser(
        "find", help="locate a CACHED transcript by id/url or title/topic fragment"
    )
    p_find.add_argument("query", help="video id, URL, or words from the title/author/topic")
    p_find.add_argument("--lang", default=None, help="restrict to a language code")
    p_find.add_argument("--limit", type=int, default=10, help="max matches to return (default: 10)")

    p_get = sub.add_parser("get", help="print a CACHED transcript (no network)")
    p_get.add_argument("target", help="YouTube URL or 11-char video id")
    p_get.add_argument("--lang", default=None, help="preferred language code (e.g. en, ru)")
    p_get.add_argument("--json", dest="as_json", action="store_true", help="print the full cached record")

    sub.add_parser("reindex", help="rebuild the lookup index from the cache")

    args = parser.parse_args(argv)

    # Offline subcommands first: they read only the local cache.
    if args.cmd == "reindex":
        print(json.dumps(cmd_reindex(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "find":
        print(json.dumps(cmd_find(args.query, args.lang, args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "get":
        vid = extract_video_id(args.target)
        if not vid:
            print(json.dumps({"status": "invalid_input", "input": args.target,
                              "message": "Could not extract a YouTube video id."}, ensure_ascii=False))
            return 0
        result = cmd_get(vid, args.lang, args.as_json)
        # For the default (text) view, print the transcript as plain text, not JSON.
        if result.get("status") == "ok" and not args.as_json:
            print(f"# {result.get('title') or vid} [{result.get('language_code')}]  {result.get('cache_file')}")
            print(result.get("text", ""))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Resolve cookie config once for the whole process.
    global _COOKIE_ARGS
    _COOKIE_ARGS = _resolve_cookie_args(
        getattr(args, "cookies_from_browser", None), getattr(args, "cookies", None)
    )

    if not YTDLP_BIN.is_file():
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": (
                        "tldw runtime not provisioned: missing .runtime/bin/yt-dlp. "
                        "Run scripts/transcript.sh once to bootstrap it."
                    ),
                }
            )
        )
        return 0

    if args.cmd == "batch":
        return cmd_batch(args)

    video_id = extract_video_id(args.target)
    if not video_id:
        print(
            json.dumps(
                {
                    "status": "invalid_input",
                    "message": (
                        "Could not extract a YouTube video id from the input. "
                        "Pass a watch/youtu.be/shorts/embed URL or an 11-char id."
                    ),
                    "input": args.target,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "list":
        result = cmd_list(video_id)
    else:
        result = cmd_fetch(video_id, args.lang)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
