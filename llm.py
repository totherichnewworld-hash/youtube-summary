#!/usr/bin/env python3
"""
Pluggable summarization backend.

Pick the provider with the LLM_PROVIDER env var:

  LLM_PROVIDER=anthropic   (default)  -> Claude, via the official Anthropic SDK
  LLM_PROVIDER=openai                 -> any OpenAI-compatible endpoint

The `openai` provider talks to OpenAI *and* every OpenAI-compatible server,
including local models, by changing OPENAI_BASE_URL:

  OpenAI:        OPENAI_BASE_URL unset (defaults to https://api.openai.com/v1)
  Groq:          https://api.groq.com/openai/v1
  OpenRouter:    https://openrouter.ai/api/v1
  Together:      https://api.together.xyz/v1
  Ollama (local):    http://localhost:11434/v1      (OPENAI_API_KEY can be anything)
  LM Studio (local): http://localhost:1234/v1
  vLLM / llama.cpp:  whatever host:port they serve

Model selection: --model / SUMMARY_MODEL, else a per-provider default.
"""

from __future__ import annotations

import os

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


def complete(system: str, user: str, model: str | None = None,
             max_tokens: int = 4096) -> str:
    """Return the model's completion for a (system, user) prompt."""
    p = provider()
    model = model or os.environ.get("SUMMARY_MODEL")
    if p == "anthropic":
        return _anthropic(system, user, model or DEFAULT_ANTHROPIC_MODEL, max_tokens)
    if p in ("openai", "local", "ollama", "openai-compatible"):
        return _openai(system, user, model or DEFAULT_OPENAI_MODEL, max_tokens)
    raise ValueError(f"Unknown LLM_PROVIDER {p!r} (use 'anthropic' or 'openai')")


# ---------------------------------------------------------------------------
# Anthropic (Claude) — official SDK, streaming so long transcripts don't time out
# ---------------------------------------------------------------------------

def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        message = stream.get_final_message()
    return "".join(b.text for b in message.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# OpenAI-compatible — covers OpenAI, hosted gateways, and local model servers
# ---------------------------------------------------------------------------

def _openai(system: str, user: str, model: str, max_tokens: int) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "The 'openai' package is required for LLM_PROVIDER=openai. "
            "Install it with: pip install openai"
        ) from e

    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),  # None -> api.openai.com
        # Local servers ignore the key but the SDK requires a non-empty value.
        api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
    )
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()
