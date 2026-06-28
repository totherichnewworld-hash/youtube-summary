#!/usr/bin/env python3
"""
Transcribe a remote audio file (e.g. a podcast episode) via a cloud API.

Both supported providers accept a *remote URL*, so the caller never has to
download the audio — important for running on GitHub Actions where the runner
should stay light. Select the provider with the TRANSCRIBER env var
("deepgram" or "assemblyai"); the API key comes from DEEPGRAM_API_KEY or
ASSEMBLYAI_API_KEY respectively.

CLI:  python transcribe.py <audio_url>
"""

from __future__ import annotations

import json as _json
import os
import re
import sys
import time

import requests


# ---------------------------------------------------------------------------
# Published transcripts (Podcasting 2.0 <podcast:transcript>) — free, no API
# ---------------------------------------------------------------------------

# Most text-friendly formats first.
_TYPE_PREFERENCE = [
    "text/plain", "application/json", "text/vtt",
    "application/x-subrip", "application/srt", "text/srt", "text/html",
]


def published_transcript(refs: list[dict]) -> str | None:
    """Fetch a transcript already published in the feed, or None if there isn't
    one we can use. Tries the most readable format first and falls back through
    the rest if a download or parse fails."""
    if not refs:
        return None

    def rank(ref: dict) -> int:
        t = ref.get("type", "")
        return _TYPE_PREFERENCE.index(t) if t in _TYPE_PREFERENCE else len(_TYPE_PREFERENCE)

    for ref in sorted(refs, key=rank):
        url = ref.get("url")
        if not url:
            continue
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
            resp.raise_for_status()
            text = _parse_transcript(resp.text, ref.get("type", ""), url)
        except Exception:
            continue
        if text and text.strip():
            return text.strip()
    return None


def _parse_transcript(body: str, mime: str, url: str) -> str:
    kind = mime
    if not kind:  # infer from the URL extension
        ext = url.lower().rsplit(".", 1)[-1] if "." in url else ""
        kind = {"vtt": "text/vtt", "srt": "application/srt", "json": "application/json",
                "html": "text/html", "txt": "text/plain"}.get(ext, "")
    if "json" in kind:
        return _parse_json_transcript(body)
    if "vtt" in kind:
        return _parse_vtt_srt(body)
    if "srt" in kind or "subrip" in kind:
        return _parse_vtt_srt(body)
    if "html" in kind:
        return re.sub(r"<[^>]+>", " ", body)
    # text/plain or unknown: VTT/SRT detection, else return as-is.
    if body.lstrip().startswith("WEBVTT") or "-->" in body[:2000]:
        return _parse_vtt_srt(body)
    return body


def _parse_json_transcript(body: str) -> str:
    """Podcast Index transcript JSON: {"segments":[{"body"/"text": "..."}]}."""
    data = _json.loads(body)
    segments = data.get("segments") if isinstance(data, dict) else None
    if not segments:
        return ""
    parts = []
    for seg in segments:
        if isinstance(seg, dict):
            piece = seg.get("body") or seg.get("text") or ""
            if piece:
                parts.append(piece.strip())
    return " ".join(parts)


def _parse_vtt_srt(body: str) -> str:
    """Strip WEBVTT/SRT timestamps, cue numbers, and tags; collapse repeats."""
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith("NOTE"):
            continue
        if "-->" in line:           # timestamp line
            continue
        if line.isdigit():          # SRT cue index
            continue
        line = re.sub(r"<[^>]+>", "", line)        # inline cue tags
        line = re.sub(r"^\s*\d+:\d+:\d+[.,]\d+\s*", "", line)
        if line and (not out or out[-1] != line):  # drop consecutive duplicates
            out.append(line)
    return "\n".join(out)

def _resolve_audio_url(audio_url: str) -> str:
    """Follow redirects to the final media URL.

    Some podcast feeds use tracking/redirect URLs (e.g. xiaoyuzhou's
    dts-api.../track/...). A cloud transcriber fetching the tracking URL may get
    the short redirect response instead of the audio, yielding an near-empty
    transcript. Resolving to the final URL ourselves avoids that.
    """
    try:
        r = requests.head(audio_url, allow_redirects=True, timeout=60,
                          headers={"User-Agent": "Mozilla/5.0"})
        if r.url and r.ok and (r.headers.get("content-type", "").startswith("audio")
                               or r.headers.get("content-type", "").startswith("video")
                               or "mp" in r.headers.get("content-type", "")):
            return r.url
    except Exception:
        pass
    return audio_url


def transcribe(audio_url: str, provider: str | None = None,
               language: str | None = None) -> str:
    provider = (provider or os.environ.get("TRANSCRIBER", "deepgram")).lower()
    audio_url = _resolve_audio_url(audio_url)
    if provider == "deepgram":
        return _deepgram(audio_url, language)
    if provider == "assemblyai":
        return _assemblyai(audio_url, language)
    raise ValueError(f"Unknown transcriber {provider!r} (use 'deepgram' or 'assemblyai')")


# ---------------------------------------------------------------------------
# Deepgram — single synchronous request with the audio URL
# ---------------------------------------------------------------------------

def _deepgram(audio_url: str, language: str | None) -> str:
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set")
    params = {"model": "nova-2", "smart_format": "true", "punctuate": "true"}
    language = language or os.environ.get("TRANSCRIBE_LANGUAGE")
    if language:
        params["language"] = language
    else:
        # nova-2 defaults to English; auto-detect so non-English audio (e.g.
        # Chinese podcasts) isn't transcribed as garbage/near-empty.
        params["detect_language"] = "true"
    resp = requests.post(
        "https://api.deepgram.com/v1/listen",
        params=params,
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
        json={"url": audio_url},
        timeout=900,  # long episodes take a while to process server-side
    )
    resp.raise_for_status()
    data = resp.json()
    return (
        data["results"]["channels"][0]["alternatives"][0]["transcript"]
    ).strip()


# ---------------------------------------------------------------------------
# AssemblyAI — submit the URL, then poll until complete
# ---------------------------------------------------------------------------

def _assemblyai(audio_url: str, language: str | None,
                poll_interval: float = 5.0, timeout: float = 3600.0) -> str:
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is not set")
    headers = {"authorization": key}

    body = {"audio_url": audio_url}
    language = language or os.environ.get("TRANSCRIBE_LANGUAGE")
    if language:
        body["language_code"] = language
    else:
        body["language_detection"] = True

    submit = requests.post(
        "https://api.assemblyai.com/v2/transcript",
        headers=headers, json=body, timeout=60,
    )
    submit.raise_for_status()
    transcript_id = submit.json()["id"]

    deadline = time.monotonic() + timeout
    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    while True:
        r = requests.get(poll_url, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        status = data["status"]
        if status == "completed":
            return (data.get("text") or "").strip()
        if status == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"AssemblyAI transcription timed out ({transcript_id})")
        time.sleep(poll_interval)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python transcribe.py <audio_url>", file=sys.stderr)
        return 2
    print(transcribe(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
