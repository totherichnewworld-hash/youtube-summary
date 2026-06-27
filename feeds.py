#!/usr/bin/env python3
"""
Resolve subscription links into normalized, pollable feeds.

You list links in `subscriptions.yaml` (YouTube channels, Apple Podcasts shows,
Spotify shows, or raw RSS URLs). This module turns each into:

    {"kind": "youtube" | "podcast", "title": str, "feed_url": str, "source": str}

so the rest of the pipeline only ever deals with RSS feeds.

Resolution rules:
  - YouTube channel/handle/URL  -> the channel's Atom RSS feed
  - Apple Podcasts show URL     -> iTunes Lookup API -> the show's real RSS feed
  - Spotify show URL            -> show title via oEmbed -> iTunes search -> RSS
                                   (best-effort; Spotify-exclusive shows have no RSS)
  - Anything else / *.xml / rss -> treated as a raw podcast RSS feed
"""

from __future__ import annotations

import re
import sys

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = 30


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(link: str) -> str:
    link = link.strip()
    if re.search(r"(youtube\.com|youtu\.be)", link, re.I):
        return "youtube"
    if "podcasts.apple.com" in link.lower():
        return "apple"
    if "open.spotify.com" in link.lower():
        return "spotify"
    return "rss"


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def resolve_youtube(link: str) -> dict:
    """A YouTube channel handle/URL/id -> its Atom RSS feed."""
    link = link.strip()

    channel_id = None
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", link):
        channel_id = link
    else:
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", link)
        if m:
            channel_id = m.group(1)

    if channel_id is None:
        # Handle or vanity URL: scrape the channel page for the channel ID.
        if link.startswith("@"):
            url = f"https://www.youtube.com/{link}"
        elif link.startswith("http"):
            url = link
        else:
            url = f"https://www.youtube.com/@{link}"
        html = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        for pat in (
            r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
            r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
        ):
            m = re.search(pat, html)
            if m:
                channel_id = m.group(1)
                break

    if channel_id is None:
        raise RuntimeError(f"Could not resolve a YouTube channel ID from {link!r}")

    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    title = _feed_title(feed_url) or channel_id
    return {"kind": "youtube", "title": title, "feed_url": feed_url, "source": link}


# ---------------------------------------------------------------------------
# Apple Podcasts  (iTunes Lookup API -> real RSS feed)
# ---------------------------------------------------------------------------

def resolve_apple(link: str) -> dict:
    m = re.search(r"id(\d+)", link)
    if not m:
        raise RuntimeError(f"Could not find a podcast id in Apple URL {link!r}")
    podcast_id = m.group(1)
    data = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": podcast_id, "entity": "podcast"},
        headers=HEADERS, timeout=TIMEOUT,
    ).json()
    results = data.get("results") or []
    if not results or not results[0].get("feedUrl"):
        raise RuntimeError(f"Apple lookup returned no feedUrl for {link!r}")
    r = results[0]
    return {
        "kind": "podcast",
        "title": r.get("collectionName") or podcast_id,
        "feed_url": r["feedUrl"],
        "source": link,
    }


# ---------------------------------------------------------------------------
# Spotify  (best-effort: title via oEmbed -> iTunes search -> RSS)
# ---------------------------------------------------------------------------

def resolve_spotify(link: str) -> dict:
    # Get the show's display name from Spotify's public oEmbed endpoint.
    oembed = requests.get(
        "https://open.spotify.com/oembed",
        params={"url": link}, headers=HEADERS, timeout=TIMEOUT,
    ).json()
    title = oembed.get("title", "").strip()
    if not title:
        raise RuntimeError(f"Could not read a show title from Spotify URL {link!r}")

    # Search the iTunes directory for a podcast with that title and reuse its RSS.
    data = requests.get(
        "https://itunes.apple.com/search",
        params={"term": title, "entity": "podcast", "limit": 5},
        headers=HEADERS, timeout=TIMEOUT,
    ).json()
    for r in data.get("results", []):
        if r.get("feedUrl"):
            return {
                "kind": "podcast",
                "title": r.get("collectionName") or title,
                "feed_url": r["feedUrl"],
                "source": link,
                "note": "resolved Spotify->RSS by title match; verify it's the right show",
            }
    raise RuntimeError(
        f"No public RSS found for Spotify show {title!r}. "
        "It may be a Spotify-exclusive (no downloadable feed)."
    )


# ---------------------------------------------------------------------------
# Raw RSS
# ---------------------------------------------------------------------------

def resolve_rss(link: str) -> dict:
    title = _feed_title(link) or link
    return {"kind": "podcast", "title": title, "feed_url": link, "source": link}


def parse_podcast_feed(feed_url: str, limit: int = 15) -> list[dict]:
    """Parse a podcast RSS feed into a list of episodes (newest first).

    Each episode: {id, title, published, audio_url, page_url, description, channel}.
    Episodes without a downloadable audio enclosure are skipped.
    """
    import feedparser

    raw = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT).content
    parsed = feedparser.parse(raw)
    channel = parsed.feed.get("title", feed_url)

    episodes = []
    for entry in parsed.entries[:limit]:
        audio_url = None
        for enc in entry.get("enclosures", []) or []:
            if str(enc.get("type", "")).startswith("audio") or enc.get("href"):
                audio_url = enc.get("href") or enc.get("url")
                if audio_url:
                    break
        if not audio_url:
            for link in entry.get("links", []) or []:
                if link.get("rel") == "enclosure" and link.get("href"):
                    audio_url = link["href"]
                    break
        if not audio_url:
            continue  # nothing to transcribe (e.g. a video-only or teaser item)

        episodes.append({
            "id": entry.get("id") or entry.get("guid") or audio_url,
            "title": entry.get("title", "(untitled)"),
            "published": entry.get("published", ""),
            "audio_url": audio_url,
            "page_url": entry.get("link", ""),
            "description": entry.get("summary", ""),
            "channel": channel,
        })
    return episodes


def _feed_title(feed_url: str) -> str | None:
    try:
        import feedparser
        parsed = feedparser.parse(
            requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT).content
        )
        return parsed.feed.get("title")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_RESOLVERS = {
    "youtube": resolve_youtube,
    "apple": resolve_apple,
    "spotify": resolve_spotify,
    "rss": resolve_rss,
}


def resolve(link: str) -> dict:
    return _RESOLVERS[classify(link)](link)


def load_subscriptions(path: str) -> list[str]:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    subs = data.get("subscriptions", [])
    if not isinstance(subs, list):
        raise ValueError("`subscriptions` must be a list of links")
    return [str(s).strip() for s in subs if str(s).strip()]


def main() -> int:
    """Resolve every link in subscriptions.yaml and print the normalized feeds."""
    path = sys.argv[1] if len(sys.argv) > 1 else "subscriptions.yaml"
    ok = True
    for link in load_subscriptions(path):
        try:
            feed = resolve(link)
            note = f"  ({feed['note']})" if feed.get("note") else ""
            print(f"[{feed['kind']:7}] {feed['title']}\n          {feed['feed_url']}{note}")
        except Exception as e:
            ok = False
            print(f"[ERROR  ] {link}\n          {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
