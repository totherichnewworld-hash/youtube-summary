# Running YouTube locally (free) + proxy option

YouTube blocks requests from cloud/datacenter IPs, so YouTube transcripts often
fail on GitHub Actions. Two ways to get them:

## Option A — Run on your own machine (free)

Your home internet is a residential IP, which YouTube doesn't block. Run the
summarizer locally; it pulls the latest state, processes your subscriptions,
and pushes the results back to the repo. Podcasts can keep running on Actions —
both write to the same `SUMMARIES.md`.

**One-off run:**
```bash
./scripts/run_local.sh
# or catch up on recent items:
./scripts/run_local.sh --backfill 1
```

**Daily automatic run (macOS):**
```bash
./scripts/install-local-schedule.sh     # runs every day at 09:00
# logs go to local-run.log; undo with ./scripts/uninstall-local-schedule.sh
```
Your Mac must be awake (or it runs at the next wake). Edit the time in
`scripts/com.youtube-summary.local.plist` if you like.

Make sure your keys are in a local `.env` (copy from `.env.example`):
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`, and `DEEPGRAM_API_KEY` if you also run
podcasts locally.

## Option B — Residential proxy on Actions (paid, set-and-forget)

If you want YouTube summarized even when your Mac is off, give Actions a
**residential** proxy (datacenter/free proxies are blocked just like the
runner). Cheapest is usually Webshare residential (~$1–3/mo).

Add as **repository secrets** (Settings → Secrets and variables → Actions):
- `WEBSHARE_PROXY_USERNAME` and `WEBSHARE_PROXY_PASSWORD`
- or a generic `YT_PROXY` = `http://user:pass@host:port`

The workflow already passes these through; no code change needed.
