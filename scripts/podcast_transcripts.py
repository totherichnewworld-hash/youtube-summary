#!/usr/bin/env python3
"""Save plain-text transcripts of a podcast's episodes from a given episode
number onward. Filenames start with the episode number, e.g. 154_<title>.txt.

Transcription uses a published feed transcript when available, otherwise
Deepgram (needs DEEPGRAM_API_KEY). Audio is never downloaded by you — the
transcriber fetches the URL.

Examples:
  export DEEPGRAM_API_KEY=...
  python scripts/podcast_transcripts.py "投资实战派" --from 154 --out zssp_transcripts
  # or pass an RSS/Apple/Spotify link instead of the name
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Allow running from anywhere: import the repo modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import feeds          # noqa: E402
import transcribe     # noqa: E402


def episode_number(title: str) -> int | None:
    """Pull an episode number from a title: 'E188 ...', '第154期', '154. ...'."""
    for pat in (r"[Ee][Pp]?\s*0*(\d{1,4})\b",   # E188 / Ep 188
                r"第\s*0*(\d{1,4})\s*期",         # 第154期
                r"^\D{0,4}0*(\d{1,4})\b"):        # leading number
        m = re.search(pat, title)
        if m:
            return int(m.group(1))
    return None


def safe(title: str) -> str:
    return re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("podcast", help="Podcast name (e.g. 投资实战派) or RSS/Apple/Spotify link")
    ap.add_argument("--from", dest="start", type=int, required=True,
                    help="First episode number to include (e.g. 154)")
    ap.add_argument("--to", dest="end", type=int, default=None,
                    help="Last episode number (default: newest)")
    ap.add_argument("--out", default="transcripts", help="Output directory")
    ap.add_argument("--language", default="zh", help="Transcription language (default: zh)")
    ap.add_argument("--limit", type=int, default=1000,
                    help="How many feed items to scan (default: 1000)")
    args = ap.parse_args()

    feed = feeds.resolve(args.podcast)
    print(f"# {feed['title']}  ({feed['feed_url']})", file=sys.stderr)
    episodes = feeds.parse_podcast_feed(feed["feed_url"], limit=args.limit)
    print(f"Feed lists {len(episodes)} episode(s).", file=sys.stderr)

    targets = []
    for ep in episodes:
        n = episode_number(ep["title"])
        if n is None or n < args.start or (args.end is not None and n > args.end):
            continue
        targets.append((n, ep))
    targets.sort(key=lambda t: t[0])  # ascending by episode number

    if not targets:
        print("No episodes matched. The feed may not list episodes that old, or "
              "the titles don't contain a number. Run without --from to see titles:",
              file=sys.stderr)
        for ep in episodes[:5]:
            print(f"   e.g. {ep['title']!r}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(targets)} episode(s) from {args.start} onward -> {out}/",
          file=sys.stderr)

    failures = 0
    for n, ep in targets:
        path = out / f"{n:03d}_{safe(ep['title'])}.txt"
        if path.exists() and path.stat().st_size > 1000:
            print(f"  skip {path.name} (already done)", file=sys.stderr)
            continue
        print(f"  • E{n}: {ep['title'][:46]} …", file=sys.stderr)
        try:
            text = (transcribe.published_transcript(ep.get("transcripts") or [])
                    or transcribe.transcribe(ep["audio_url"], language=args.language))
        except Exception as e:
            print(f"    ! failed (skipping): {e}", file=sys.stderr)
            failures += 1
            continue
        if len(text.strip()) < 50:
            print(f"    ! transcript suspiciously short ({len(text)} chars), skipping",
                  file=sys.stderr)
            failures += 1
            continue
        header = (f"{ep['title']}\n{ep.get('published', '')}\n"
                  f"{ep.get('page_url') or ep['audio_url']}\n{'=' * 60}\n\n")
        path.write_text(header + text.strip() + "\n", encoding="utf-8")
        print(f"    -> {path}  ({len(text):,} chars)", file=sys.stderr)

    print(f"Done. {len(targets) - failures} saved, {failures} failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
