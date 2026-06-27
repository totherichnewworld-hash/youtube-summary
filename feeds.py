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
from xml.etree import ElementTree as ET

import requests

ATOM = "{http://www.w3.org/2005/Atom}"

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
    # A bare name (no URL scheme, no domain dot, not a @handle) -> look it up by
    # name in the podcast directory.
    if "://" not in link and "." not in link and not link.startswith("@"):
        return "search"
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
# Search by name  (iTunes podcast directory -> RSS feed)
# ---------------------------------------------------------------------------

def resolve_search(name: str) -> dict:
    """Resolve a bare podcast NAME to its RSS feed via the iTunes directory."""
    data = requests.get(
        "https://itunes.apple.com/search",
        params={"term": name, "entity": "podcast", "limit": 10},
        headers=HEADERS, timeout=TIMEOUT,
    ).json()
    results = [r for r in data.get("results", []) if r.get("feedUrl")]
    if not results:
        raise RuntimeError(
            f"No podcast found for name {name!r}. "
            "Paste its Apple Podcasts, Spotify, or RSS link instead."
        )
    # Prefer an exact (case-insensitive) name match; otherwise take the top hit.
    target = name.strip().casefold()
    best = next(
        (r for r in results if (r.get("collectionName") or "").strip().casefold() == target),
        results[0],
    )
    return {
        "kind": "podcast",
        "title": best.get("collectionName") or name,
        "feed_url": best["feedUrl"],
        "source": name,
        "note": "resolved by name search — verify it's the right show",
    }


# ---------------------------------------------------------------------------
# Raw RSS
# ---------------------------------------------------------------------------

def resolve_rss(link: str) -> dict:
    title = _feed_title(link) or link
    return {"kind": "podcast", "title": title, "feed_url": link, "source": link}


def parse_podcast_feed(feed_url: str, limit: int = 15) -> list[dict]:
    """Parse a podcast RSS/Atom feed into episodes (newest first), stdlib only.

    Each episode: {id, title, published, audio_url, page_url, description, channel}.
    Episodes without a downloadable audio enclosure are skipped.
    """
    raw = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT).content
    root = ET.fromstring(raw)

    channel = root.find("channel")  # RSS 2.0
    if channel is not None:
        channel_title = channel.findtext("title") or feed_url
        item_nodes = channel.findall("item")
    else:                            # Atom fallback
        channel_title = root.findtext(f"{ATOM}title") or feed_url
        item_nodes = root.findall(f"{ATOM}entry")

    episodes = []
    for item in item_nodes[:limit]:
        audio_url = _enclosure_url(item)
        if not audio_url:
            continue  # nothing to transcribe (e.g. a teaser or video-only item)
        if channel is not None:  # RSS 2.0 fields are unqualified
            episodes.append({
                "id": (item.findtext("guid") or "").strip() or audio_url,
                "title": item.findtext("title") or "(untitled)",
                "published": item.findtext("pubDate") or "",
                "audio_url": audio_url,
                "page_url": item.findtext("link") or "",
                "description": item.findtext("description") or "",
                "channel": channel_title,
            })
        else:                    # Atom fields
            episodes.append({
                "id": item.findtext(f"{ATOM}id") or audio_url,
                "title": item.findtext(f"{ATOM}title") or "(untitled)",
                "published": item.findtext(f"{ATOM}published")
                             or item.findtext(f"{ATOM}updated") or "",
                "audio_url": audio_url,
                "page_url": _atom_link(item),
                "description": item.findtext(f"{ATOM}summary") or "",
                "channel": channel_title,
            })
    return episodes


def _enclosure_url(item: ET.Element) -> str | None:
    """Find an audio enclosure URL in an RSS <item> or Atom <entry>."""
    # RSS 2.0 <enclosure url="..." type="audio/...">
    encs = item.findall("enclosure")
    audio = [e for e in encs if str(e.get("type", "")).startswith("audio")]
    for e in (audio or encs):
        if e.get("url"):
            return e.get("url")
    # Atom <link rel="enclosure" href="..." type="audio/...">
    for link in item.findall(f"{ATOM}link"):
        if link.get("rel") == "enclosure" and link.get("href"):
            return link.get("href")
    return None


def _atom_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{ATOM}link"):
        if link.get("rel") in (None, "alternate") and link.get("href"):
            return link.get("href")
    return ""


def _feed_title(feed_url: str) -> str | None:
    try:
        raw = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT).content
        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is not None:
            return channel.findtext("title")
        return root.findtext(f"{ATOM}title")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_RESOLVERS = {
    "youtube": resolve_youtube,
    "apple": resolve_apple,
    "spotify": resolve_spotify,
    "search": resolve_search,
    "rss": resolve_rss,
}


def resolve(link: str) -> dict:
    return _RESOLVERS[classify(link)](link)


def load_subscriptions(path: str) -> list[dict]:
    """Load subscriptions, each as {"url": str, "prompt": str | None}.

    Two entry forms are accepted and may be mixed in the same list:

        subscriptions:
          - https://www.youtube.com/@handle          # simple: default prompt

          - url: https://www.youtube.com/@other       # custom inline prompt
            prompt: |
              请聚焦事实而非观点，聚焦投资而非政治。

          - url: https://feeds.example.com/show.xml   # custom prompt from a file
            prompt_file: prompts/investing.txt

    A relative prompt_file is looked up in the current directory first, then
    next to the subscriptions file. `prompt` is None when no custom prompt is
    set, so the caller falls back to the default prompt.txt.
    """
    from pathlib import Path
    import yaml

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    subs = data.get("subscriptions", [])
    if not isinstance(subs, list):
        raise ValueError("`subscriptions` must be a list")

    base = Path(path).resolve().parent
    result: list[dict] = []
    for entry in subs:
        if isinstance(entry, str):
            url = entry.strip()
            if url:
                result.append({"url": url, "prompt": None})
            continue
        if isinstance(entry, dict):
            url = str(entry.get("url") or entry.get("link") or "").strip()
            if not url:
                raise ValueError(f"subscription entry is missing a url: {entry!r}")
            prompt = entry.get("prompt")
            if prompt is not None:
                prompt = str(prompt)
            elif entry.get("prompt_file"):
                pf = Path(entry["prompt_file"])
                if not pf.is_absolute() and not pf.exists():
                    pf = base / pf
                prompt = pf.read_text(encoding="utf-8")
            result.append({"url": url, "prompt": prompt})
            continue
        raise ValueError(f"subscription entry must be a link or a mapping: {entry!r}")
    return result


def main() -> int:
    """Resolve every link in subscriptions.yaml and print the normalized feeds."""
    path = sys.argv[1] if len(sys.argv) > 1 else "subscriptions.yaml"
    ok = True
    for sub in load_subscriptions(path):
        link = sub["url"]
        try:
            feed = resolve(link)
            note = f"  ({feed['note']})" if feed.get("note") else ""
            tag = "  [custom prompt]" if sub.get("prompt") else ""
            print(f"[{feed['kind']:7}] {feed['title']}{tag}\n"
                  f"          {feed['feed_url']}{note}")
        except Exception as e:
            ok = False
            print(f"[ERROR  ] {link}\n          {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
