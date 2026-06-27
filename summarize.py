#!/usr/bin/env python3
"""
Summarize new YouTube videos from a channel using Claude.

Workflow:
  1. Resolve a channel handle (e.g. @ltshijie) to its channel ID.
  2. Poll the channel's public RSS feed for the latest uploads.
  3. For any video not seen before, fetch its transcript.
  4. Summarize the transcript with Claude, using a customizable prompt.
  5. Save each summary to a markdown file (and print it).

State (which videos have been seen) is kept in a JSON file so the script
only summarizes genuinely new uploads. Designed to be run on a schedule
(cron / Task Scheduler / systemd timer).

Requires the ANTHROPIC_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via CLI flags or environment variables)
# ---------------------------------------------------------------------------

DEFAULT_CHANNEL = os.environ.get("YT_CHANNEL", "@ltshijie")
DEFAULT_MODEL = os.environ.get("YT_MODEL", "claude-opus-4-8")
DEFAULT_PROMPT_FILE = os.environ.get("YT_PROMPT_FILE", "prompt.txt")
DEFAULT_STATE_FILE = os.environ.get("YT_STATE_FILE", "state.json")
DEFAULT_OUTPUT_DIR = os.environ.get("YT_OUTPUT_DIR", "summaries")
DEFAULT_LANGUAGES = os.environ.get("YT_LANGUAGES", "en,zh-Hans,zh-Hant,zh").split(",")
# Summary length cap (output token budget). Friendly name SUMMARY_MAX_TOKENS,
# with YT_MAX_TOKENS kept as a fallback for older configs.
DEFAULT_MAX_TOKENS = int(
    os.environ.get("SUMMARY_MAX_TOKENS")
    or os.environ.get("YT_MAX_TOKENS")
    or "4096"
)
# Rough safety cap: Chinese ~1 char/token, English ~4 chars/token.
# 16 000 chars keeps the transcript well under 12 000 tokens on Groq free tier.
DEFAULT_MAX_TRANSCRIPT_CHARS = int(os.environ.get("YT_MAX_TRANSCRIPT_CHARS", "16000"))
# Transcript cleanup is done chunk-by-chunk so each call stays small enough for
# small free-tier rate limits. Small chunks ~= small output, no truncation.
DEFAULT_CORRECTION_CHUNK_CHARS = int(os.environ.get("YT_CORRECTION_CHUNK_CHARS", "2500"))
DEFAULT_CORRECTION_MAX_TOKENS = int(os.environ.get("YT_CORRECTION_MAX_TOKENS", "4096"))

ATOM = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Channel resolution + feed parsing
# ---------------------------------------------------------------------------

def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def resolve_channel_id(channel: str) -> str:
    """Turn an @handle, channel URL, or raw UC... id into a channel ID."""
    channel = channel.strip()

    # Already a channel ID.
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel):
        return channel

    # A /channel/UC... URL.
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", channel)
    if m:
        return m.group(1)

    # Normalize a handle into a full URL to scrape.
    if channel.startswith("@"):
        url = f"https://www.youtube.com/{channel}"
    elif channel.startswith("http"):
        url = channel
    else:
        url = f"https://www.youtube.com/@{channel}"

    html = _http_get(url)
    for pattern in (
        r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
        r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
        r'channel_id=(UC[A-Za-z0-9_-]{22})',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)

    raise RuntimeError(
        f"Could not resolve a channel ID from {channel!r}. "
        "Pass the UC... channel ID directly with --channel."
    )


def extract_video_id(s: str) -> str:
    """Pull an 11-char video ID out of a URL or accept a bare ID."""
    s = s.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    for pattern in (
        r"[?&]v=([A-Za-z0-9_-]{11})",          # watch?v=ID
        r"youtu\.be/([A-Za-z0-9_-]{11})",      # youtu.be/ID
        r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})",
    ):
        m = re.search(pattern, s)
        if m:
            return m.group(1)
    raise ValueError(f"Could not extract a video ID from {s!r}")


def fetch_video_meta(video_id: str) -> dict:
    """Best-effort title/channel via YouTube's oEmbed endpoint."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    meta = {"video_id": video_id, "title": video_id, "channel": "",
            "published": "", "url": url}
    try:
        data = json.loads(_http_get(
            f"https://www.youtube.com/oembed?url={url}&format=json"))
        meta["title"] = data.get("title", video_id)
        meta["channel"] = data.get("author_name", "")
    except Exception:
        pass  # oEmbed is a nicety; summary still works without it
    return meta


def fetch_feed(channel_id: str) -> list[dict]:
    """Return the channel's recent uploads from its Atom RSS feed, newest first."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = _http_get(feed_url)
    root = ET.fromstring(xml)

    channel_name = root.findtext(f"{ATOM}title") or channel_id
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        video_id = entry.findtext(f"{YT_NS}videoId")
        if not video_id:
            continue
        title = entry.findtext(f"{ATOM}title") or "(untitled)"
        published = entry.findtext(f"{ATOM}published") or ""
        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "published": published,
                "channel": channel_name,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos


# ---------------------------------------------------------------------------
# Transcript fetching
# ---------------------------------------------------------------------------

def fetch_transcript(video_id: str, languages: list[str]) -> str:
    """Fetch a transcript, trying the requested languages, then any available."""
    from youtube_transcript_api import YouTubeTranscriptApi

    def join(segments) -> str:
        parts = []
        for seg in segments:
            text = seg["text"] if isinstance(seg, dict) else getattr(seg, "text", "")
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    # Newer youtube-transcript-api (>=1.0) uses an instance .fetch() API;
    # older versions expose static .get_transcript(). Support both.
    api = YouTubeTranscriptApi()

    if hasattr(api, "fetch"):
        try:
            return join(api.fetch(video_id, languages=languages))
        except Exception:
            # Fall back to whatever transcript exists in any language.
            transcripts = api.list(video_id)
            transcript = next(iter(transcripts))
            return join(transcript.fetch())

    # Legacy static API.
    try:
        return join(YouTubeTranscriptApi.get_transcript(video_id, languages=languages))
    except Exception:
        listing = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = next(iter(listing))
        return join(transcript.fetch())


# ---------------------------------------------------------------------------
# Summarization with Claude
# ---------------------------------------------------------------------------

def summarize(meta: dict, transcript: str, prompt_template: str,
              model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS,
              max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS) -> str:
    import llm

    if len(transcript) > max_transcript_chars:
        transcript = transcript[:max_transcript_chars] + "\n\n[transcript truncated]"

    # Substitute placeholders without str.format so free-form custom prompts
    # (which may contain stray { } characters) never crash.
    instructions = prompt_template
    for key in ("title", "channel", "published", "url"):
        instructions = instructions.replace("{" + key + "}", str(meta.get(key, "")))
    # Ensure the model always sees the basic context even if a custom prompt
    # didn't include any placeholders.
    header = (
        f"Title: {meta.get('title', '')}\n"
        f"Channel: {meta.get('channel', '')}\n"
        f"Published: {meta.get('published', '')}\n"
        f"URL: {meta.get('url', '')}"
    )
    user_content = f"{header}\n\n{instructions}\n\n---\nTranscript:\n{transcript}"
    system = (
        "You write clear, accurate summaries of video and podcast transcripts. "
        "Stay faithful to the source and never invent details."
    )
    # Backend (Claude / OpenAI-compatible / local) is chosen by LLM_PROVIDER.
    return llm.complete(system, user_content, model=model, max_tokens=max_tokens)


CORRECTION_SYSTEM = (
    "You clean up raw, auto-generated transcripts for readability. "
    "Add correct punctuation, capitalization, and paragraph breaks; fix obvious "
    "transcription mistakes; and remove filler and accidental repetitions. "
    "Keep the SAME language as the input. Do NOT translate, summarize, omit, or "
    "add any content — preserve every point and all the meaning. "
    "Output only the cleaned transcript, nothing else."
)


def correct_transcript(transcript: str, model: str | None = None,
                       max_tokens: int = DEFAULT_CORRECTION_MAX_TOKENS,
                       chunk_chars: int = DEFAULT_CORRECTION_CHUNK_CHARS) -> str:
    """Rewrite a raw transcript into readable prose, chunk by chunk.

    The text is split into small chunks so each LLM call stays well under
    free-tier token/rate limits and the output is never truncated. Content is
    preserved — this only improves punctuation, paragraphs, and obvious errors.
    """
    import llm

    text = transcript.strip()
    if not text:
        return text
    chunks = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    cleaned_parts = []
    for chunk in chunks:
        cleaned = llm.complete(
            CORRECTION_SYSTEM,
            f"Clean up this transcript segment:\n\n{chunk}",
            model=model, max_tokens=max_tokens,
        )
        if cleaned.strip():
            cleaned_parts.append(cleaned.strip())
    return "\n\n".join(cleaned_parts)


# ---------------------------------------------------------------------------
# State + output
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"channel_id": None, "seen": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def write_summary(output_dir: Path, meta: dict, summary: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\- ]+", "", meta["title"]).strip().replace(" ", "_")[:60]
    fname = f"{meta['published'][:10] or 'undated'}_{meta['video_id']}_{safe_title}.md"
    path = output_dir / fname
    body = (
        f"# {meta['title']}\n\n"
        f"- **Channel:** {meta['channel']}\n"
        f"- **Published:** {meta['published']}\n"
        f"- **URL:** {meta['url']}\n"
        f"- **Summarized:** {datetime.now(timezone.utc).isoformat()}\n\n"
        f"---\n\n{summary}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def _safe_filename(meta: dict) -> str:
    safe_title = re.sub(r"[^\w\- ]+", "", meta["title"]).strip().replace(" ", "_")[:60]
    return f"{meta['published'][:10] or 'undated'}_{meta['video_id']}_{safe_title}"


def write_transcript(output_dir: Path, meta: dict, transcript: str) -> Path:
    """Save the full (untruncated) transcript next to its summary, as plain text."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_safe_filename(meta)}.txt"
    header = (
        f"{meta['title']}\n"
        f"{meta['channel']} — {meta['published']}\n"
        f"{meta['url']}\n"
        f"{'=' * 60}\n\n"
    )
    path.write_text(header + transcript.strip() + "\n", encoding="utf-8")
    return path


def write_document(output_dir: Path, meta: dict, summary: str | None,
                   transcript: str) -> Path:
    """Write the readable .md: optional summary section + the full transcript.

    This is the single page you open per item — summary first (if one was made),
    then the complete transcript below it.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_safe_filename(meta)}.md"
    parts = [
        f"# {meta['title']}",
        "",
        f"- **Channel:** {meta['channel']}",
        f"- **Published:** {meta['published']}",
        f"- **URL:** {meta['url']}",
        f"- **Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    if summary and summary.strip():
        parts += ["---", "", "## 摘要 Summary", "", summary.strip(), ""]
    if transcript and transcript.strip():
        parts += ["---", "", "## 全文 Transcript", "", transcript.strip(), ""]
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file)
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")
    state = load_state(state_path)

    print(f"Resolving channel {args.channel!r}...", file=sys.stderr)
    channel_id = resolve_channel_id(args.channel)
    state["channel_id"] = channel_id
    print(f"  channel_id = {channel_id}", file=sys.stderr)

    videos = fetch_feed(channel_id)
    print(f"Feed has {len(videos)} recent video(s).", file=sys.stderr)

    seen = set(state.get("seen", []))
    new_videos = [v for v in videos if v["video_id"] not in seen]

    # First run: seed state instead of summarizing the whole backlog,
    # unless --initial N asks for the N newest to be summarized now.
    first_run = not seen
    if first_run and args.initial == 0:
        state["seen"] = [v["video_id"] for v in videos]
        save_state(state_path, state)
        print(
            f"First run: marked {len(videos)} existing video(s) as seen. "
            "New uploads from now on will be summarized. "
            "Use --initial N to summarize the N newest now.",
            file=sys.stderr,
        )
        return 0

    if first_run and args.initial > 0:
        new_videos = videos[: args.initial]

    if not new_videos:
        print("No new videos.", file=sys.stderr)
        return 0

    # Feed is newest-first; process oldest-first so summaries land in order.
    output_dir = Path(args.output_dir)
    exit_code = 0
    for meta in reversed(new_videos):
        print(f"\n=== {meta['title']} ({meta['url']}) ===", file=sys.stderr)
        try:
            transcript = fetch_transcript(meta["video_id"], args.languages)
        except Exception as e:
            print(f"  ! No transcript available, skipping: {e}", file=sys.stderr)
            # Mark as seen so we don't retry a video that will never have captions.
            seen.add(meta["video_id"])
            state["seen"] = sorted(seen)
            save_state(state_path, state)
            exit_code = 1
            continue

        try:
            summary = summarize(
                meta, transcript, prompt_template, args.model, args.max_tokens
            )
        except Exception as e:
            print(f"  ! Summarization failed (will retry next run): {e}", file=sys.stderr)
            exit_code = 1
            continue

        out_path = write_summary(output_dir, meta, summary)
        print(f"\n{summary}\n", file=sys.stdout)
        print(f"  -> saved {out_path}", file=sys.stderr)

        seen.add(meta["video_id"])
        state["seen"] = sorted(seen)
        save_state(state_path, state)

    return exit_code


def cmd_videos(args: argparse.Namespace) -> int:
    """Summarize an explicit list of video URLs/IDs (ad-hoc; ignores state)."""
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")
    output_dir = Path(args.output_dir)
    exit_code = 0

    for raw in args.videos:
        try:
            video_id = extract_video_id(raw)
        except ValueError as e:
            print(f"! {e}", file=sys.stderr)
            exit_code = 1
            continue

        meta = fetch_video_meta(video_id)
        print(f"\n=== {meta['title']} ({meta['url']}) ===", file=sys.stderr)

        try:
            transcript = fetch_transcript(video_id, args.languages)
        except Exception as e:
            print(f"  ! No transcript available, skipping: {e}", file=sys.stderr)
            exit_code = 1
            continue

        try:
            summary = summarize(
                meta, transcript, prompt_template, args.model, args.max_tokens
            )
        except Exception as e:
            print(f"  ! Summarization failed: {e}", file=sys.stderr)
            exit_code = 1
            continue

        out_path = write_summary(output_dir, meta, summary)
        print(f"\n{summary}\n", file=sys.stdout)
        print(f"  -> saved {out_path}", file=sys.stderr)

    return exit_code


def cmd_resolve(args: argparse.Namespace) -> int:
    print(resolve_channel_id(args.channel))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--channel", default=DEFAULT_CHANNEL,
                        help="Channel @handle, URL, or UC... id (default: %(default)s)")

    run = sub.add_parser("run", parents=[common],
                         help="Check the feed and summarize new videos")
    run.add_argument("--prompt-file", dest="prompt_file", default=DEFAULT_PROMPT_FILE,
                     help="File with the customizable summary prompt (default: %(default)s)")
    run.add_argument("--state-file", dest="state_file", default=DEFAULT_STATE_FILE)
    run.add_argument("--output-dir", dest="output_dir", default=DEFAULT_OUTPUT_DIR)
    run.add_argument("--model", default=None, help="Override model (else provider default)")
    run.add_argument("--max-tokens", dest="max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    run.add_argument("--languages", type=lambda s: s.split(","), default=DEFAULT_LANGUAGES,
                     help="Comma-separated transcript language preference")
    run.add_argument("--initial", type=int, default=0, metavar="N",
                     help="On first run, summarize the N newest videos (default: 0 = seed only)")
    run.set_defaults(func=cmd_run)

    vids = sub.add_parser("videos",
                          help="Summarize specific video URLs/IDs (ignores state)")
    vids.add_argument("videos", nargs="+", metavar="URL_OR_ID",
                      help="One or more YouTube URLs or 11-char video IDs")
    vids.add_argument("--prompt-file", dest="prompt_file", default=DEFAULT_PROMPT_FILE)
    vids.add_argument("--output-dir", dest="output_dir", default=DEFAULT_OUTPUT_DIR)
    vids.add_argument("--model", default=None, help="Override model (else provider default)")
    vids.add_argument("--max-tokens", dest="max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    vids.add_argument("--languages", type=lambda s: s.split(","), default=DEFAULT_LANGUAGES)
    vids.set_defaults(func=cmd_videos)

    res = sub.add_parser("resolve", parents=[common],
                         help="Print the resolved channel ID and exit")
    res.set_defaults(func=cmd_resolve)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except urllib.error.URLError as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
