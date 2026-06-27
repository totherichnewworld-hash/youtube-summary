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

import os
import sys
import time

import requests

def transcribe(audio_url: str, provider: str | None = None,
               language: str | None = None) -> str:
    provider = (provider or os.environ.get("TRANSCRIBER", "deepgram")).lower()
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
    if language:
        params["language"] = language
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
