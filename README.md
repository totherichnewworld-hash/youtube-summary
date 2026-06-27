# Subscription summarizer

> **Goal:** get an emailed summary whenever a channel/podcast you follow
> (YouTube, Apple Podcasts, or Spotify) publishes something new — run
> automatically on GitHub Actions.

## Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Subscription list + link resolver (`feeds.py`) | ✅ done |
| 2 | YouTube path: many channels → transcript → summary | ✅ engine built (`summarize.py`) |
| 3 | Podcast path: RSS → cloud transcription (Deepgram/AssemblyAI) → summary | ⏳ next |
| 4 | Email delivery + GitHub Actions schedule + state persistence | ⏳ |
| 5 | Subscription import (Takeout/Spotify OAuth), Spotify-exclusive notes-only | ⏳ |

Chosen stack: **sources** = YouTube + podcast RSS · **transcription** = cloud API
· **delivery** = email · **runtime** = GitHub Actions.

## Phase 1 — subscriptions

List the things you follow as plain links in `subscriptions.yaml` (copy
`subscriptions.example.yaml`). The resolver turns each link into a normalized
feed:

```bash
cp subscriptions.example.yaml subscriptions.yaml
# edit subscriptions.yaml to add your links, then validate:
python feeds.py subscriptions.yaml
```

Each line can be a YouTube channel (`@handle` or `UC...`), an Apple Podcasts
show URL, a Spotify show URL, or a raw RSS feed. Apple/Spotify links are
resolved to the show's underlying RSS feed (Spotify via a best-effort
title match — verify the result). Spotify-*exclusive* shows have no public
feed and can only be handled notes-only (Phase 5).

---

## YouTube channel summarizer (engine)

Watches a YouTube channel and, whenever a **new video** is uploaded, fetches its
transcript and writes a summary with Claude. The summary prompt is fully
customizable.

Default channel: [`@ltshijie`](https://www.youtube.com/@ltshijie).

## How it works

1. Resolves the channel `@handle` to its channel ID.
2. Polls the channel's public RSS feed (`youtube.com/feeds/videos.xml`) — no
   API key or quota needed for discovery.
3. Tracks which videos it has already summarized in `state.json`.
4. For each new video, pulls the transcript via `youtube-transcript-api`.
5. Summarizes the transcript with Claude (`claude-opus-4-8`) using the prompt in
   `prompt.txt`.
6. Saves a markdown summary under `summaries/` and prints it to stdout.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

```bash
# First run: seed state with current uploads (does NOT summarize the backlog).
python summarize.py run

# ...or summarize the 3 newest videos right away on the first run:
python summarize.py run --initial 3

# Every later run summarizes only videos uploaded since the last run:
python summarize.py run

# Use a different channel:
python summarize.py run --channel @someoneelse
python summarize.py run --channel UCxxxxxxxxxxxxxxxxxxxxxx

# Just print the resolved channel ID:
python summarize.py resolve --channel @ltshijie
```

### Summarize specific videos (ad-hoc)

To summarize an explicit list of videos instead of watching a channel — handy
for testing or one-offs — use `videos`. It accepts full URLs (including ones
with extra `&list=` / `&start_radio=` params), `youtu.be` short links, or bare
11-char IDs, and ignores `state.json`:

```bash
python summarize.py videos \
  https://www.youtube.com/watch?v=tJewzQT8ANI \
  https://www.youtube.com/watch?v=5E75Wj4lXZE \
  yJtckcMHM2g
```

## Customizing the summary prompt

Edit **`prompt.txt`** — this is the instruction Claude follows. It supports these
placeholders, filled in per video:

`{title}`, `{channel}`, `{published}`, `{url}`

The transcript is appended automatically after your prompt, so write the prompt
as instructions only. Examples:

- *"Summarize in exactly 5 bullet points, in Chinese."*
- *"Extract every actionable tip and list them as a checklist."*
- *"Give me a one-paragraph executive summary plus a list of any tools, books, or people mentioned."*

Point at a different prompt file with `--prompt-file my_prompt.txt`.

## Configuration

Flags (or environment variables) you can set:

| Flag | Env var | Default |
|------|---------|---------|
| `--channel` | `YT_CHANNEL` | `@ltshijie` |
| `--model` | `YT_MODEL` | `claude-opus-4-8` |
| `--prompt-file` | `YT_PROMPT_FILE` | `prompt.txt` |
| `--state-file` | `YT_STATE_FILE` | `state.json` |
| `--output-dir` | `YT_OUTPUT_DIR` | `summaries` |
| `--languages` | `YT_LANGUAGES` | `en,zh-Hans,zh-Hant,zh` |
| `--max-tokens` | `YT_MAX_TOKENS` | `4096` |
| `--initial` | — | `0` (seed only on first run) |

`--languages` is the transcript preference order; if none match, the first
available transcript is used.

## Running on a schedule

The script is stateless between runs apart from `state.json`, so just run it on
a timer.

**cron** (check every 30 minutes):

```cron
*/30 * * * * cd /path/to/youtube-summary && ANTHROPIC_API_KEY=sk-ant-... /usr/bin/python3 summarize.py run >> run.log 2>&1
```

**systemd timer**, **launchd**, **Task Scheduler**, or a GitHub Actions
`schedule:` workflow all work the same way — invoke `python summarize.py run` on
your interval. Commit/persist `state.json` if the environment is ephemeral so it
remembers what it has already summarized.

## Notes

- Discovery uses the RSS feed, which lists roughly the 15 most recent uploads.
  Running at least daily ensures nothing is missed.
- Videos without captions are skipped and marked seen (so they aren't retried
  forever); a summarization error leaves the video unseen so it's retried next run.
- Transcript fetching depends on YouTube being reachable from wherever you run
  this.
