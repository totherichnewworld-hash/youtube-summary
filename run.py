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


def deliver(item: dict, summary: str, output_dir: Path) -> None:
    meta = {
        "title": item["title"], "channel": item["channel"],
        "published": item["published"], "video_id": item["id"], "url": item["url"],
    }
    path = summarize.write_summary(output_dir, meta, summary)
    print(f"  -> saved {path}", file=sys.stderr)

    if notify.email_configured():
        subject = f"[{item['channel']}] {item['title']}"
        body = (
            f"{item['title']}\n{item['channel']} — {item['published']}\n"
            f"{item['url']}\n\n{summary}\n"
        )
        notify.send_email(subject, body)
        print("  -> emailed", file=sys.stderr)
    else:
        print(f"\n{summary}\n", file=sys.stdout)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subscriptions", default="subscriptions.yaml")
    p.add_argument("--prompt-file", default="prompt.txt")
    p.add_argument("--state-file", default="state.json")
    p.add_argument("--output-dir", default="summaries")
    p.add_argument("--model", default=None, help="Override model (else provider default)")
    p.add_argument("--max-tokens", type=int, default=summarize.DEFAULT_MAX_TOKENS)
    p.add_argument("--languages", type=lambda s: s.split(","),
                   default=summarize.DEFAULT_LANGUAGES)
    p.add_argument("--limit", type=int, default=10,
                   help="Max recent items to inspect per feed")
    p.add_argument("--initial", type=int, default=0, metavar="N",
                   help="On first run, summarize the N newest items per feed")
    args = p.parse_args()

    state_path = Path(args.state_file)
    output_dir = Path(args.output_dir)
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    first_run = not state.get("initialized")

    sub_path = args.subscriptions
    if not Path(sub_path).exists() and Path("subscriptions.example.yaml").exists():
        print(f"{sub_path} not found; falling back to subscriptions.example.yaml",
              file=sys.stderr)
        sub_path = "subscriptions.example.yaml"
    links = feeds.load_subscriptions(sub_path)
    print(f"{len(links)} subscription(s).", file=sys.stderr)

    exit_code = 0
    for link in links:
        try:
            feed = feeds.resolve(link)
        except Exception as e:
            print(f"! Could not resolve {link}: {e}", file=sys.stderr)
            exit_code = 1
            continue

        print(f"\n# {feed['title']} ({feed['kind']})", file=sys.stderr)
        try:
            items = collect_items(feed, args.limit)
        except Exception as e:
            print(f"  ! Could not read feed: {e}", file=sys.stderr)
            exit_code = 1
            continue

        new_items = [it for it in items if it["id"] not in seen]
        if first_run:
            # Seed: mark everything seen; summarize only the N newest if asked.
            for it in items:
                seen.add(it["id"])
            new_items = items[: args.initial] if args.initial > 0 else []

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
            try:
                summary = summarize.summarize(
                    item, transcript, prompt_template, args.model, args.max_tokens
                )
            except Exception as e:
                print(f"    ! summarize failed (retry next run): {e}", file=sys.stderr)
                exit_code = 1
                continue  # leave unseen so it's retried

            deliver(item, summary, output_dir)
            seen.add(item["id"])
            state["seen"] = sorted(seen)
            save_state(state_path, state)

    state["seen"] = sorted(seen)
    state["initialized"] = True
    save_state(state_path, state)

    if first_run and args.initial == 0:
        print("First run: seeded state; new items from now on will be summarized.",
              file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
