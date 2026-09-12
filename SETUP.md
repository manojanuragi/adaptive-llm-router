# Setup & Usage Guide

A step-by-step runbook for getting the Adaptive LLM Router (ALR) running
and actually using it — from a zero-dependency taste test up through a
deployed HTTP API with your own auth, a cloud trace store, and a
scheduled savings benchmark. For the full technical reference (how
routing decisions work, every metric definition, honest limitations),
see [README.md](README.md) — this file is the practical "do this, then
this" path.

---

## 0. Prerequisites

- Python 3.11 or 3.12
- `git`
- Optional, depending on which pieces you want:
  - [Ollama](https://ollama.com) — for real local-model (Tier 1) execution
  - The `claude` CLI ([Claude Code](https://claude.com/claude-code)) — for the default Tier-2 frontier connector
  - Docker + Docker Compose — for containerized deployment
  - A GitHub account with Actions enabled — for the scheduled savings benchmark

---

## 1. Get the code

```bash
git clone <this-repo-url>
cd adaptive-llm-router
```

## 2. Zero-dependency taste test (no execution, just routing decisions)

```bash
pip install pyyaml
python3 demo.py
```

This prints, for a handful of sample tasks, which tier (tool / local /
frontier) the router picks and why — no API keys, no servers, nothing
else installed.

## 3. Run the test suite

```bash
python3 -m unittest discover -s tests -v
```

All core tests run on stdlib + PyYAML only — no network, no credentials,
no Docker required. A handful of autoscaling-specific tests auto-skip if
Redis/Postgres aren't reachable (see README "Testing the autoscaling
path" if you want to run those too).

## 4. Choose how to authenticate the frontier tier (Claude)

You need **one** of these three. None are baked into the repo — every
user configures their own.

| Option | Setup | Billed against |
|---|---|---|
| **A. Interactive CLI** (simplest, for your own machine) | Run `claude login` once. Nothing else to configure. | Your Claude Pro/Max subscription |
| **B. Headless CLI** (for servers/containers/CI with no interactive session) | Run `claude setup-token` once (needs an interactive login the first time), then set the printed value as `CLAUDE_CODE_OAUTH_TOKEN` wherever the app runs. | Your Claude Pro/Max subscription |
| **C. Raw API key** (optional alternative to A/B) | Set `ALR_FRONTIER_TRANSPORT=api` and `ANTHROPIC_API_KEY=sk-ant-...` | Metered Anthropic API usage |

**Never paste a token, key, or connection string into a chat with an AI
assistant or commit it to git** — treat all three the same way you'd
treat a password.

Pick A if you're just trying this out locally. Pick B or C only once
you're deploying somewhere headless (Docker, a server, GitHub Actions).

## 5. (Optional) Set up a local model for Tier 1

Skip this if you're fine with every non-trivial task escalating straight
to the frontier tier.

```bash
# install Ollama: https://ollama.com
ollama pull llama3.2:3b     # or any model you prefer
# if you pick a different model, edit alr/config/models.yaml ->
# models.local-coder.model_name to match
```

Without Ollama running, local-tier calls fail closed (not crash) and the
pipeline reports `success=False` with the reason in the output — that's
expected, not a bug, if you skipped this step.

## 6. Run the full pipeline with real execution

```bash
python3 pipeline_demo.py
```

This runs a few real tasks end-to-end (analyze → route → execute →
validate → escalate → trace) using whatever you configured in step 4/5,
and prints a rollup of cost, success rate, and (new) per-caller usage.

## 7. Run the HTTP API

```bash
pip install -r requirements.txt

# Required for any real deployment — comma-separated keys, optionally
# labeled key:identity for per-caller usage tracking (see step 9):
export ALR_API_KEYS="your-key-here"

uvicorn alr.api:app --reload
```

**The app will refuse to start** if it has a cloud-capable model
registered (the default config always does) but no usable Claude
credential from step 4 — that's intentional (see README "What changed to
add per-caller usage tracking and startup auth enforcement"), not a bug.
Fix step 4 if you see this.

## 8. Use the API

```bash
# Dry run — routing decision only, no execution
curl -X POST localhost:8000/route -H 'Content-Type: application/json' \
  -H "x-api-key: $ALR_API_KEYS" \
  -d '{"task": "git status"}'

# Full execution
curl -X POST localhost:8000/execute -H 'Content-Type: application/json' \
  -H "x-api-key: $ALR_API_KEYS" \
  -d '{"task": "Summarize these logs"}'

# Metrics
curl localhost:8000/metrics -H "x-api-key: $ALR_API_KEYS"        # cheap rollup
curl localhost:8000/metrics/full -H "x-api-key: $ALR_API_KEYS"   # full evaluation metric set
```

## 9. (Optional) Give each caller their own identity

If more than one person/service will call your deployment, label each
key instead of sharing one:

```bash
export ALR_API_KEYS="key-for-alice:alice,key-for-bob:bob"
```

Every response from `/execute` now includes `caller_id`, and
`GET /metrics` / `/metrics/full` include a `usage_by_caller` breakdown —
requests, cost, and cost saved per person. The label is never a secret
(it's just shown in your own trace data) — the raw key itself is never
logged or returned.

## 10. (Optional) Move the trace store to the cloud

By default every task/cost/savings/caller record lives in a local SQLite
file (`alr_traces.db`). To make it persistent and visible from anywhere,
without any code change:

1. Sign up at [neon.com](https://neon.com) (permanent free tier, no
   credit card) and create a project.
2. Copy its connection string. **Set it as an environment variable in
   your own terminal — never paste it into a chat:**
   ```bash
   export DATABASE_URL="postgresql://...your-neon-connection-string..."
   ```
3. Run the app as usual — `psycopg[binary]` is already in
   `requirements.txt`. Verify it actually switched backends:
   ```bash
   python3 -c "from alr.tracing import build_trace_store; print(type(build_trace_store()).__name__)"
   # -> PostgresTraceStore  (not TraceStore, which means it's still SQLite)
   ```

## 11. (Optional) Deploy with Docker

```bash
cp .env.example .env
# edit .env: set ALR_API_KEYS, and (since this Dockerfile has no `claude`
# CLI installed) ALR_FRONTIER_TRANSPORT=api + ANTHROPIC_API_KEY

docker compose up --build

curl localhost:8000/health
curl localhost:8000/ready
```

See README "Deploying it" and "Autoscaling" for multi-replica setup
(Postgres + Redis shared across replicas) and Kubernetes manifests.

## 12. (Optional) Enable the scheduled savings benchmark

Runs a tiny fixed task set every 6 hours and commits the cost/token
savings numbers to `benchmarks/history/` in this repo.

1. Push this repo (including `.github/workflows/benchmark.yml`) to
   GitHub — a workflow that only exists locally doesn't run.
2. Add **one** of these as a repository secret (Settings → Secrets and
   variables → Actions):
   - `CLAUDE_CODE_OAUTH_TOKEN` (recommended — from `claude setup-token`,
     billed against your subscription), or
   - `ANTHROPIC_API_KEY` (billed as API usage)
3. Trigger it once manually to confirm it works: Actions tab →
   "Scheduled Savings Benchmark" → **Run workflow**.

If the final `git push` step in that workflow fails with a permissions
error, check Settings → Actions → General → **Workflow permissions** is
set to "Read and write permissions."

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App refuses to start with "No frontier auth configured" | Finish step 4 — no Claude credential is usable for the resolved transport. |
| Local-tier tasks return `success=False`, "Could not reach local LLM" | Ollama isn't running, or you skipped step 5 — this is a graceful failure, not a crash. |
| `claude CLI reported an error (HTTP 401): ... OAuth access token is invalid` / `Invalid bearer token` | The `CLAUDE_CODE_OAUTH_TOKEN` is wrong, stale, or corrupted in transit (check for an embedded newline/whitespace from copy-paste — `[[ "$CLAUDE_CODE_OAUTH_TOKEN" == *[[:space:]]* ]] && echo "has whitespace"`). Mint a fresh one with `claude setup-token`. |
| `/execute` always returns `caller_id: "anonymous"` | `ALR_API_KEYS` isn't set, or the key you're sending doesn't match one you configured. |
| Scheduled benchmark workflow doesn't appear under the Actions tab | It exists locally but hasn't been pushed to GitHub yet — see step 12.1. |

## Where to go next

- [README.md](README.md) — full technical reference: how routing
  decisions are made, every evaluation metric's precise definition,
  honest limitations, and the Kubernetes/autoscaling path.
- `benchmarks/compare_direct_vs_router.py` — a reproducible cost +
  accuracy comparison: calling Claude directly vs. routing through ALR.
