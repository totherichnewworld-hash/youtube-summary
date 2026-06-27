#!/usr/bin/env python3
"""
The integrator: check every subscription for new items and email a summary.

For each link in subscriptions.yaml:
  - YouTube channel -> RSS -> new videos -> caption transcript -> summary
  - Podcast (Apple/Spotify/RSS) -> RSS -> new episodes -> cloud transcription
    of the audio URL -> summary

Already-summarized items are tracked in state.json so only genuinely new items
are processed. Summaries are emailed if SMTP is configured (see notify.py),
and always saved as markdown under summaries/.

Designed to run on a schedule (GitHub Actions). On the very first run it seeds
state without summarizing the backlog, unless --initial N is given.

Env: ANTHROPIC_API_KEY (required); DEEPGRAM_API_KEY or ASSEMBLYAI_API_KEY for
podcasts; SMTP_* for email (optional).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import feeds
import notify
import summarize
import transcribe


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen": [], "initialized": False}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def collect_items(feed: dict, limit: int) -> list[dict]:
    """Return a feed's recent items as uniform dicts (newest first)."""
    if feed["kind"] == "youtube":
        items = []
        channel_id = feed["feed_url"].split("channel_id=")[-1]
        for v in summarize.fetch_feed(channel_id)[:limit]:
            items.append({
                "kind": "youtube",
                "id": v["video_id"],
                "title": v["title"],
                "channel": v["channel"],
                "published": v["published"],
                "url": v["url"],
                "video_id": v["video_id"],
            })
        return items

    items = []
    for ep in feeds.parse_podcast_feed(feed["feed_url"], limit=limit):
        items.append({
            "kind": "podcast",
            "id": ep["id"],
            "title": ep["title"],
            "channel": ep["channel"],
            "published": ep["published"],
            "url": ep["page_url"] or ep["audio_url"],
            "audio_url": ep["audio_url"],
            "description": ep["description"],
        })
    return items


def get_transcript(item: dict, languages: list[str]) -> str:
    if item["kind"] == "youtube":
        return summarize.fetch_transcript(item["video_id"], languages)
    return transcribe.transcribe(item["audio_url"])


def deliver(item: dict, summary: str | None, transcript: str, output_dir: Path,
            transcript_dir: Path | None) -> None:
    meta = {
        "title": item["title"], "channel": item["channel"],
        "published": item["published"], "video_id": item["id"], "url": item["url"],
    }
    # One readable page: summary (if made) + the full transcript.
    path = summarize.write_document(output_dir, meta, summary, transcript)
    print(f"  -> saved {path}", file=sys.stderr)

    if transcript_dir is not None and transcript:
        tpath = summarize.write_transcript(transcript_dir, meta, transcript)
        print(f"  -> saved transcript {tpath}", file=sys.stderr)

    # Push channels are independent; each fires only if it's configured. The
    # document is always saved above (read it on GitHub), so no channel needs to
    # be set up. A full transcript is too long to push, so when there's no
    # summary we push a short heads-up with the link instead.
    subject = f"[{item['channel']}] {item['title']}"
    head = f"{item['title']}\n{item['channel']} — {item['published']}\n{item['url']}"
    if summary and summary.strip():
        body = f"{head}\n\n{summary}\n"
    else:
        body = f"{head}\n\n（全文已保存，未生成摘要 — full transcript saved.)\n"
    pushed = False
    if notify.email_configured():
        notify.send_email(subject, body)
        print("  -> emailed", file=sys.stderr)
        pushed = True
    if notify.discord_configured():
        notify.send_discord(subject, body)
        print("  -> posted to Discord", file=sys.stderr)
        pushed = True
    if not pushed and summary and summary.strip():
        print(f"\n{summary}\n", file=sys.stdout)


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def build_index(output_dir: Path, index_path: Path) -> None:
    """(Re)build a single Markdown index of every saved summary, newest first.

    Open this one page on GitHub for your daily read; each row links to the
    full Chinese summary. Safe to run every time — it just rewrites the page.
    """
    from urllib.parse import quote

    rows = []
    for md in output_dir.glob("*.md"):
        title = channel = published = ""
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and not title:
                title = line[2:].strip()
            elif line.startswith("- **Channel:**"):
                channel = line.split("**Channel:**", 1)[1].strip()
            elif line.startswith("- **Published:**"):
                published = line.split("**Published:**", 1)[1].strip()
        rows.append((published, channel, title or md.stem, md.name))

    rows.sort(reverse=True)  # ISO-ish dates sort correctly as strings
    lines = [
        "# 摘要 Summaries",
        "",
        f"_{len(rows)} summaries — newest first. Updated automatically._",
        "",
        "| Date | Channel | Title |",
        "|------|---------|-------|",
    ]
    for published, channel, title, name in rows:
        link = f"{output_dir.name}/{quote(name)}"
        lines.append(f"| {published[:10] or '—'} | {channel} | [{title}]({link}) |")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subscriptions", default="subscriptions.yaml")
    p.add_argument("--prompt-file", default="prompt.txt")
    p.add_argument("--state-file", default="state.json")
    p.add_argument("--output-dir", default="summaries")
    p.add_argument("--index-file", default="SUMMARIES.md",
                   help="Markdown index of all summaries for your daily read")
    p.add_argument("--transcript-dir", default="transcripts",
                   help="Where to save full transcripts (default: %(default)s)")
    p.add_argument("--save-transcripts", action=argparse.BooleanOptionalAction,
                   default=_env_flag("SAVE_TRANSCRIPTS", True),
                   help="Save the full transcript next to each summary "
                        "(env: SAVE_TRANSCRIPTS; default: on)")
    p.add_argument("--summary", action=argparse.BooleanOptionalAction,
                   default=_env_flag("MAKE_SUMMARY", True),
                   help="Write a summary (env: MAKE_SUMMARY; default: on). "
                        "Use --no-summary for transcript-only.")
    p.add_argument("--correct-transcript", action=argparse.BooleanOptionalAction,
                   default=_env_flag("CORRECT_TRANSCRIPT", False),
                   help="Clean up the transcript for readability with the LLM "
                        "(env: CORRECT_TRANSCRIPT; default: off — uses more tokens).")
    p.add_argument("--model", default=None, help="Override model (else provider default)")
    p.add_argument("--max-tokens", type=int, default=summarize.DEFAULT_MAX_TOKENS)
    p.add_argument("--max-transcript-chars", type=int,
                   default=summarize.DEFAULT_MAX_TRANSCRIPT_CHARS,
                   help="Truncate transcripts to this many chars before summarizing")
    p.add_argument("--languages", type=lambda s: s.split(","),
                   default=summarize.DEFAULT_LANGUAGES)
    p.add_argument("--limit", type=int, default=10,
                   help="Max recent items to inspect per feed")
    p.add_argument("--initial", type=int, default=0, metavar="N",
                   help="On first run, summarize the N newest items per feed")
    p.add_argument("--backfill", type=int, default=0, metavar="N",
                   help="Summarize the N newest items per feed right now, even "
                        "if already seen/initialized (for catching up on past "
                        "videos). Reachable items are limited to what the feed "
                        "still lists (~15 newest for YouTube).")
    args = p.parse_args()

    state_path = Path(args.state_file)
    output_dir = Path(args.output_dir)
    transcript_dir = Path(args.transcript_dir) if args.save_transcripts else None
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    first_run = not state.get("initialized")

    sub_path = args.subscriptions
    if not Path(sub_path).exists() and Path("subscriptions.example.yaml").exists():
        print(f"{sub_path} not found; falling back to subscriptions.example.yaml",
              file=sys.stderr)
        sub_path = "subscriptions.example.yaml"
    subs = feeds.load_subscriptions(sub_path)
    print(f"{len(subs)} subscription(s).", file=sys.stderr)

    exit_code = 0
    for sub in subs:
        link = sub["url"]
        # Per-subscription prompt overrides the shared default prompt.txt.
        sub_prompt = sub.get("prompt") or prompt_template
        try:
            feed = feeds.resolve(link)
        except Exception as e:
            print(f"! Could not resolve {link}: {e}", file=sys.stderr)
            exit_code = 1
            continue

        print(f"\n# {feed['title']} ({feed['kind']})", file=sys.stderr)
        # Scan enough items to satisfy a backfill/initial request.
        scan_limit = max(args.limit, args.initial, args.backfill)
        try:
            items = collect_items(feed, scan_limit)
        except Exception as e:
            print(f"  ! Could not read feed: {e}", file=sys.stderr)
            exit_code = 1
            continue

        new_items = [it for it in items if it["id"] not in seen]
        if first_run:
            # Seed the backlog as seen, but leave the N newest we're about to
            # summarize unseen so a failed summary is retried next run (they get
            # marked seen only after successful delivery below).
            new_items = items[: args.initial] if args.initial > 0 else []
            to_summarize = {it["id"] for it in new_items}
            for it in items:
                if it["id"] not in to_summarize:
                    seen.add(it["id"])
        if args.backfill > 0:
            # Explicit catch-up: summarize the N newest items regardless of
            # whether they've been seen. Wins over the new/first-run selection.
            new_items = items[: args.backfill]

        # Process oldest-first so summaries arrive in chronological order.
        for item in reversed(new_items):
            print(f"  • {item['title']}", file=sys.stderr)
            try:
                transcript = get_transcript(item, args.languages)
            except Exception as e:
                print(f"    ! no transcript, skipping: {e}", file=sys.stderr)
                seen.add(item["id"])  # don't retry items that will never transcribe
                exit_code = 1
                continue

            # Optional: clean up the transcript for readability. On failure,
            # fall back to the raw transcript rather than losing the item.
            if args.correct_transcript:
                try:
                    transcript = summarize.correct_transcript(
                        transcript, args.model)
                    print("    -> transcript cleaned up", file=sys.stderr)
                except Exception as e:
                    print(f"    ! cleanup failed, using raw transcript: {e}",
                          file=sys.stderr)
                    exit_code = 1

            summary = None
            if args.summary:
                try:
                    summary = summarize.summarize(
                        item, transcript, sub_prompt, args.model,
                        args.max_tokens, args.max_transcript_chars,
                    )
                except Exception as e:
                    print(f"    ! summarize failed (retry next run): {e}",
                          file=sys.stderr)
                    exit_code = 1
                    continue  # leave unseen so it's retried

            deliver(item, summary, transcript, output_dir, transcript_dir)
            seen.add(item["id"])
            state["seen"] = sorted(seen)
            save_state(state_path, state)

    state["seen"] = sorted(seen)
    state["initialized"] = True
    save_state(state_path, state)

    # Refresh the index page so the daily-read list stays current.
    if output_dir.exists() and any(output_dir.glob("*.md")):
        build_index(output_dir, Path(args.index_file))

    if first_run and args.initial == 0:
        print("First run: seeded state; new items from now on will be summarized.",
              file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
