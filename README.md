# Adaptive LLM Router (ALR) — Phase 1 MVP

A working implementation of the Phase-1 MVP from the design doc (section 28):
a rule-based router that sends each task to the cheapest tier that can
handle it — **deterministic tool → local LLM → frontier LLM (Claude)** —
with confidence-based escalation, a context firewall, trace logging, a
full evaluation-metrics framework (sections 16–18), and horizontal
autoscaling support.

## What's actually implemented vs. stubbed

| Component | Status |
|---|---|
| Task Analyzer (keyword/heuristic classification) | ✅ real, tested |
| Model Registry (YAML-backed) | ✅ real, tested |
| Rule-based Router (Strategy A) | ✅ real, tested |
| Tier 0 deterministic tools (git, grep, json/csv parse, tests) | ✅ real, tested, path-sandboxed |
| Tier 1 local LLM connector (Ollama) | ✅ real code, sync + async, retry/circuit-breaker; needs Ollama running to execute |
| Tier 2 frontier LLM connector (Claude) | ✅ real code, sync + async, retry/circuit-breaker; **two selectable transports** — shell out to the `claude` CLI, billed against your Claude Pro/Max subscription (interactive `claude login`, or headless via `CLAUDE_CODE_OAUTH_TOKEN`), or call the raw Anthropic Messages API directly (optional, `ANTHROPIC_API_KEY`) — see "Two ways to run Tier 2" below |
| Token/cost savings tracking (per response + rolled up) | ✅ real, tested — `Pipeline._compute_savings()`; see "Token & cost savings tracking" below |
| Scheduled savings benchmark, committed to git every 6h | ✅ real — `.github/workflows/benchmark.yml` + `benchmarks/record_savings.py`; see "Scheduled savings benchmark" below |
| Confidence/escalation logic | ✅ real, tested (heuristic signals — see "Honest limitations") |
| Context firewall (compression + progressive disclosure) | ✅ real, tested, wired into frontier calls when `payload["context_text"]` is set |
| Policy engine (section 21) | ✅ real, tested — deny/require-approval on high-risk patterns, enforced inside the pipeline |
| Auth (API keys) | ✅ real, tested — supports optional `key:identity` labels for per-caller usage tracking (see "Per-caller usage tracking" below) |
| Frontier auth startup enforcement | ✅ real, tested — `validate_frontier_auth()` refuses to start the API if no usable Claude credential is configured, instead of failing confusingly on the first request |
| Rate limiting | ✅ real, tested — **Redis-backed and verified across independently running containers** when `REDIS_URL` is set; falls back to in-memory (single-process) otherwise |
| Retry + circuit breaker | ✅ real, tested |
| Trace store + rollup metrics | ✅ real, tested — **Postgres backend verified against a live `postgres:16-alpine` instance**, including a concurrent-startup race that was found and fixed (see Autoscaling section); SQLite remains the single-replica default |
| Evaluation framework (sections 16, 17, 18) | ✅ real, tested — `alr/evaluation.py`: full metric set, pass@k, baseline comparison harness (see Evaluation Metrics section) |
| FastAPI HTTP layer (auth, rate limit, size limits, lifespan, health/ready) | ✅ real, tested against live HTTP traffic |
| Dockerfile + docker-compose + Kubernetes manifests + CI | ✅ **image built and run**; multi-replica scaling verified with real containers (see Autoscaling section); k8s manifests are standard-pattern and YAML-validated, not yet applied to a live cluster in this environment |
| Learned classifier / contextual bandit / RL (Strategies B–E) | ❌ not built — Phase 3+ per the doc's own roadmap |
| Model competition, router memory, dynamic model marketplace | ❌ not built — "Advanced Features" in the doc, intentionally deferred |
| Approval-workflow UI (Slack/queue/human-in-the-loop) | ❌ not built — `PolicyEngine.approval_callback` is a hook for you to wire up |

99 tests pass. 90 of them use only the Python standard library + PyYAML
(no network, no API keys, no local model server, no Docker required) —
that count includes `test_frontier_transport.py` (CLI/API dispatch,
frontier-auth startup validation, with `urllib`/`subprocess` mocked) and
`test_savings.py` (token/cost savings tracking). 9 more exercise the
Redis/Postgres-backed autoscaling path against real containers and
auto-skip if those aren't reachable (see "Testing the autoscaling path"
below). Run them yourself: `python3 -m unittest discover -s tests -v`.

## Quick start

**Requirements:** Python 3.11 or 3.12 (matches the CI matrix — other 3.x
versions likely work but aren't tested).

```bash
# 0. Get the code
git clone <this-repo-url>
cd adaptive-llm-router

# 1. Zero-dependency taste test — routing decisions only, no execution
pip install pyyaml   # if you don't have it
python3 demo.py

# 2. Run the test suite
python3 -m unittest discover -s tests -v

# 3. Real execution (needs Ollama running and/or the `claude` CLI logged in —
#    see "Two ways to run Tier 2" below; no API key required)
python3 pipeline_demo.py

# 4. HTTP API
pip install -r requirements.txt
uvicorn alr.api:app --reload
curl -X POST localhost:8000/route -H 'Content-Type: application/json' \
  -d '{"task": "git status"}'
curl -X POST localhost:8000/execute -H 'Content-Type: application/json' \
  -d '{"task": "Summarize these logs"}'
curl localhost:8000/metrics        # cheap rollup (section 16)
curl localhost:8000/metrics/full   # full evaluation metric set (sections 16-18)
```

## Set up a local model (optional, for real Tier-1 execution)

```bash
# install Ollama: https://ollama.com
ollama pull qwen2.5-coder:32b     # or any model you prefer
# edit alr/config/models.yaml -> models.local-coder.model_name to match
```

Without Ollama running, Tier-1 calls fail closed (`LocalLLMUnavailable`)
and the pipeline returns `success=False` with the error message rather
than crashing — check `result.output` in that case.

## Two ways to run Tier 2: Claude CLI (your subscription) vs. the raw Anthropic API

`alr/frontier_llm.py` supports two transports for calling the frontier
model, selected with the `ALR_FRONTIER_TRANSPORT` env var (or pinned per
model via `transport:` in `models.yaml`) — same public functions
(`call_frontier_llm`, `call_frontier_llm_async`, `FrontierLLMResponse`)
either way, so nothing else in the pipeline changes. **`ANTHROPIC_API_KEY`
is entirely optional** — both the interactive and headless variants of
the default `cli` transport bill against your existing Claude Pro/Max
subscription instead of raw API usage:

| Transport | `ALR_FRONTIER_TRANSPORT` | Auth | Billing | Best for |
|---|---|---|---|---|
| **CLI, interactive** (default) | `cli` or unset | `claude login` session | Your Claude Pro/Max subscription | Running the router directly on your own machine |
| **CLI, headless** (default) | `cli` or unset | `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) | Your Claude Pro/Max subscription | Docker/Kubernetes/CI without an interactive login, while still using your subscription, not API billing |
| **Raw API** (optional) | `api` | `ANTHROPIC_API_KEY` | Raw Anthropic API usage | Anyone who'd rather bill through the API instead of a subscription |

### CLI transport (default) — subscription billing, interactive or headless

Shells out to the `claude` CLI (Claude Code) in non-interactive print mode:

```
claude -p "<task text>" --output-format json --model claude-sonnet-5 --restricted
```

`--restricted` disables Claude Code's own Bash/file tools so each call is
a plain single-turn completion, not an agent run. The CLI's JSON output
reports `total_cost_usd` directly, which `pipeline.py` uses as the
authoritative cost for that call (falling back to the registry's blended
`cost_per_1k_tokens` estimate only if that field is ever missing). This
transport never touches `ANTHROPIC_API_KEY` — the `claude` CLI
authenticates itself, and `frontier_llm.py` just inherits whatever
environment the CLI is already using.

**Two ways to authenticate this transport, same code path either way:**
- **Interactive** — run `claude login` once on the machine running the
  router. Nothing else to configure.
- **Headless** (Docker, Kubernetes, CI/scheduled jobs — no interactive
  session available) — run `claude setup-token` once *interactively,
  anywhere* to mint a long-lived OAuth token, then set it as
  `CLAUDE_CODE_OAUTH_TOKEN` in the headless environment (e.g. a GitHub
  Actions secret). The `claude` binary picks this env var up on its own;
  `subprocess.run()` in `frontier_llm.py` inherits the parent process's
  environment by default, so no code change is needed to use it. This is
  what `.github/workflows/benchmark.yml` uses by default — see
  "Scheduled savings benchmark" below. **Either way, this bills against
  your Claude Pro/Max subscription, not per-token API usage.**

**Requirements:** the `claude` binary on `PATH`, plus one of the two auth
methods above.

**Trade-offs to know before you rely on this:**
- **Cost includes Claude Code's own overhead.** Each call carries Claude
  Code's system prompt/tool-definition prefix (even in `--restricted`
  mode), which is billed like any other input tokens the first time and
  then usually served from Anthropic's prompt cache on subsequent calls
  within the cache TTL — so cost per call can vary noticeably depending
  on how recently another `claude` call ran on the same machine.
- **Adds subprocess latency.** Spinning up the CLI per call is slower
  than a raw HTTPS request — fine for a router doing a handful of
  escalations, not ideal for high-throughput frontier traffic.
- **Subscription usage still has limits** (Pro/Max plan quotas) — high
  request volume can hit those before it would hit a pay-as-you-go API
  budget. Switch to the API transport below if you need metered,
  uncapped-by-plan billing instead.

### API transport (optional) — raw Anthropic API usage

Set `ALR_FRONTIER_TRANSPORT=api` and `ANTHROPIC_API_KEY`, and
`frontier_llm.py` calls `https://api.anthropic.com/v1/messages` directly
over `urllib` (stdlib only, no `anthropic` SDK dependency, consistent
with `local_llm.py`'s Ollama connector). This is entirely optional — the
headless CLI transport above (subscription billing) covers the same
headless/CI use case without needing an API key at all. Reach for this
only if you specifically want raw API billing instead.

```bash
export ALR_FRONTIER_TRANSPORT=api
export ANTHROPIC_API_KEY=sk-ant-...
python3 pipeline_demo.py   # or uvicorn alr.api:app, etc.
```

The Messages API doesn't return a billed-dollars figure the way the
Claude CLI's JSON output does, so cost always falls back to the
registry's `cost_per_1k_tokens` estimate for this transport (`cost_usd`
is always `None` from `_call_frontier_llm_api`).

You can also pin the transport per model regardless of the env var by
adding `transport: api` (or `cli`) under that model's entry in
`alr/config/models.yaml`.

## Project layout

```
alr/
  models.py       Core dataclasses (TaskEnvelope, RouteDecision, ExecutionResult, ...)
  registry.py     Loads config/models.yaml — never hard-code model names elsewhere
  config/
    models.yaml   Model registry + capability-matrix priors + escalation thresholds
  analyzer.py     Task Analyzer — keyword/heuristic classification (section 8)
  tools.py        Tier 0 — deterministic tools: git, grep, test runner, parsers (path-sandboxed)
  local_llm.py    Tier 1 — Ollama connector, sync + async (stdlib urllib, no SDK dependency)
  frontier_llm.py Tier 2 — dual transport: Claude CLI (subprocess) or raw Anthropic API (urllib), sync + async; see "Two ways to run Tier 2"
  router.py       Rule-based Adaptive Routing Engine (Strategy A, section 9)
  validator.py    Confidence scoring + escalation decision (section 10)
  context.py      Context firewall + progressive disclosure (sections 12, 23)
  policy.py       Policy engine — deny/require-approval on high-risk operations (section 21)
  auth.py         API key verification
  rate_limit.py   Token-bucket rate limiter — in-memory (single-process) or Redis-backed (shared across replicas)
  retry.py        Retry with backoff + circuit breaker
  logging_config.py  Structured JSON logging
  tracing.py      SQLite (default) + Postgres trace store, rollup metrics (section 16)
  evaluation.py   Full evaluation metric set (sections 16-18), pass@k, baseline comparison harness (section 35)
  pipeline.py     Wires it all together: the core loop from section 41, sync + async paths
  api.py          FastAPI HTTP layer (auth, rate limit, size limits, lifespan, health/ready, metrics/full)
tests/
  test_core.py                 Unit tests: analyzer, router, validator, context, tools
  test_pipeline.py              Integration tests: full pipeline with mocked LLM calls
  test_deployment_readiness.py  Policy engine, auth, rate limiter, retry/breaker, path safety, async pipeline
  test_evaluation.py            Evaluation metrics, pass@k, baseline comparison harness
  test_scaling_infra.py         Registry/token-count regressions + live Redis/Postgres autoscaling tests (auto-skip if unreachable)
  test_frontier_transport.py    CLI/API transport dispatch + raw Anthropic API connector (mocked urllib)
  test_savings.py                Pipeline._compute_savings — token/cost savings tracking
demo.py               Zero-dependency routing-decisions-only demo
pipeline_demo.py      Full pipeline demo with real execution
benchmarks/
  compare_direct_vs_router.py  Direct Claude vs. adaptive router — cost + accuracy (see "Benchmark" section)
  record_savings.py            Scheduled savings snapshot — run by benchmark.yml every 6h (see "Scheduled savings benchmark")
  history/                      Committed savings track record: savings_history.jsonl (raw) + SUMMARY.md (table)
Dockerfile            Non-root, healthcheck, workspace-sandboxed
docker-compose.yml    App (N replicas) + nginx load balancer + Postgres + Redis + Ollama — scale-ready by default
nginx.conf            Round-robin load balancer config for docker-compose --scale
k8s/                  Kubernetes manifests: Deployment, Service, HPA, ConfigMap, PDB, demo Postgres/Redis
.github/workflows/ci.yml         Test suite + Docker build on every push
.github/workflows/benchmark.yml  Scheduled savings benchmark, every 6h — see "Scheduled savings benchmark"
```

Each module only depends on the ones below it in this list (tools/connectors
→ router → pipeline → api), so you can swap any single piece — e.g. replace
`router.py`'s rule-based Strategy A with a learned classifier (Strategy B
from the doc) — without touching anything else.

## How routing actually works right now

1. **Tool match wins outright.** If the task text matches a Tier-0 pattern
   (`git status`, `grep`, etc.), it always runs as a deterministic tool —
   never an LLM call, section 5's "prefer deterministic tools whenever
   possible."
2. **Privacy is a hard constraint.** `privacy=restricted` never reaches a
   cloud model, regardless of complexity (section 20).
3. **High complexity or high risk skips straight to frontier.** No point
   paying the local-model round trip if it's very likely to be escalated
   anyway.
4. **Everything else defaults to local-first,** then gets validated after
   execution.
5. **Escalation:** the local model is prompted to self-report a confidence
   score; combined with a registry prior and hedge-language detection
   (`"I'm not sure"`, `"it's hard to say"`, etc.), if the blended confidence
   falls below `escalation.confidence_threshold` (default 0.70 in
   `models.yaml`), the *same task* is re-routed to frontier automatically —
   this is the pipeline's core value proposition and it's tested in
   `test_pipeline.py::test_low_confidence_local_result_escalates_to_frontier`.

## Evaluation Metrics

`alr/evaluation.py` implements the full metric set from sections 16
(Observability), 17 (Evaluation Framework), and 18 (Critical Metric:
False-Local Rate) of the design doc — not just the headline numbers in
`GET /metrics`.

### Live metrics: `GET /metrics/full`

```bash
curl localhost:8000/metrics/full -H "x-api-key: $ALR_API_KEYS"
```

Returns every metric below, computed from the trace store (SQLite or
Postgres, whichever `DATABASE_URL` selects):

| Group | Metrics |
|---|---|
| **Quality** | `task_success_rate`, `local_success_rate`, `frontier_success_rate`, `model_failure_rate` |
| **Economics** | `total_cost`, `cost_per_task`, `cost_per_successful_task`, `frontier_tokens_total`/`_per_task`, `local_tokens_total`, `total_tokens_per_task` |
| **Performance** | `latency_p50_ms`, `latency_p95_ms` (overall and broken out `_by_route`), `time_to_successful_completion_p50_ms` |
| **Routing** (section 17/18) | `escalation_rate`, `false_local_rate`, `false_frontier_rate`, `correct_routing_rate` |
| **Context Firewall** (sections 12, 23, 32-C) | `context_compression_ratio`, `context_samples` |
| **Composite** (section 16) | `quality_per_dollar`, `quality_per_second` |
| Breakdown | `requests_by_route` |

**False-local rate** uses direct evidence (a local-routed task that
failed or got escalated). **False-frontier rate** needs the model
registry's capability-matrix priors to estimate what local would have
scored — it's `None` from the live endpoint's default call unless you
pass a registry, which `api.py` already does (`_pipeline.registry`).
Both are explicitly proxies, exactly as design-doc section 15 warns
("replace with measured numbers once you have real outcome data") — see
"Honest limitations" below for the precise definitions and caveats.

### Section 35's baseline comparison, in code

The doc's own benchmark design (section 35: compare Frontier-only vs.
Local-only vs. Adaptive routing on the same task set) is a couple of
lines:

```python
from alr.evaluation import compare_baselines
from alr.models import TaskEnvelope
from alr.pipeline import Pipeline

pipeline = Pipeline()
tasks = [TaskEnvelope(task=t) for t in your_benchmark_task_list]

results = compare_baselines(pipeline, tasks)  # {"frontier_only": ..., "local_only": ..., "adaptive": ...}
for mode, metrics in results.items():
    print(mode, metrics.task_success_rate, metrics.total_cost, metrics.requests_by_route)
```

`run_baseline(pipeline, tasks, mode="local_only")` runs a single
strategy; `compare_baselines` runs all three (or a subset via `modes=`)
and returns one `EvaluationMetrics` per mode. Neither touches the
pipeline's real trace store — rows are aggregated purely in memory, so
you can benchmark repeatedly without polluting production trace history.
Tool-tier tasks (`git status`, etc.) stay on the tool tier in every
baseline — forcing "frontier-only" doesn't turn a deterministic
operation into an LLM call, matching section 5's tool-first principle.

### Pass@k (section 17, Quality group)

Pass@k needs multiple sampled attempts per task, which a single-shot
production trace stream doesn't have by construction — it's a
benchmark-time metric, not something `/metrics/full` computes from live
traffic:

```python
from alr.evaluation import pass_at_k_from_attempts

# task_id -> list of per-attempt success booleans (e.g. from sampling the
# local model N times at temperature > 0 and checking each attempt)
attempts = {"task-1": [True, False, False], "task-2": [True, True, False]}
pass_at_k_from_attempts(attempts, k=1)
```

## Benchmark: direct Claude vs. the adaptive router

A real, reproducible run comparing **calling Claude directly (no router)**
against **routing through ALR** on 2 tasks, using the CLI-based connector
above (`llama3.2:3b` via Ollama for Tier 1, `claude-sonnet-5` via the
`claude` CLI for Tier 2). Reproduce it yourself:
`python3 benchmarks/compare_direct_vs_router.py`.

Both tasks were graded objectively — string/regex match against a known
ground truth, not an LLM judge — so "accuracy" below is a hard pass/fail:

| Task | Direct Claude | Adaptive Router | Accuracy (both) |
|---|---|---|---|
| `log_extraction` (log_analysis; ground truth = 3 named exceptions) | frontier, **$0.02463**, 7.9s | escalated to frontier, **$0.01204**, 20.4s | 1.00 / 1.00 |
| `code_debugging` (an off-by-one bug with a known correct fix) | frontier, **$0.02284**, 7.2s | **resolved locally, $0.00000**, 7.7s | 1.00 / 1.00 |
| **Total** | **$0.04748** | **$0.01204** | **1.00 / 1.00** |

Headline: **74.6% cost reduction, zero accuracy loss** on this run — but
two caveats matter more than that single number:

1. **The `log_extraction` cost gap is mostly a prompt-cache artifact, not
   a routing win.** The router correctly judged the local model couldn't
   reliably do structured extraction and escalated to frontier anyway —
   same tier as the direct call. The $0.01204 vs. $0.02463 difference is
   Claude Code's own prompt cache being warm from the immediately
   preceding direct call in the same benchmark run. Spread the same
   requests out in production and expect that task's routed cost to land
   close to the direct cost, not half of it.
2. **The real, unconfounded win is `code_debugging`: 100% of that call's
   cost avoided for an identically correct answer.** Rerunning that exact
   task afterward produced a *different* outcome — the local model's
   self-reported confidence came out lower that time and it escalated
   anyway (cost $0.008, still correct). That's not benchmark noise, it's
   the limitation directly below: local self-reported confidence is
   heuristic and uncalibrated, so the same task can resolve locally one
   run and escalate the next. **Expect savings to vary run to run**,
   proportional to how much of your real workload the local model can
   actually handle — not a fixed percentage.

Across all 3 trials (2 in the run above + 1 rerun), the router never
produced a less accurate answer than calling Claude directly — it either
matched it at the same cost (correctly escalating) or matched it for
free (correctly staying local). That is the actual value proposition:
never worse, sometimes free — not a guaranteed discount.

## Token & cost savings tracking

Every response the pipeline produces gets a savings figure attached —
not just an aggregate benchmark number, but per-response, in real time.
`Pipeline._compute_savings()` (`alr/pipeline.py`) runs on every `run()`/
`run_async()` result right before it's traced, and populates five new
`ExecutionResult` fields:

| Field | Meaning |
|---|---|
| `baseline_cost` | What the registry's frontier model would have cost for this same response |
| `cost_saved` | `baseline_cost - cost` |
| `cost_saved_pct` | `cost_saved` as a percentage of `baseline_cost` |
| `tokens_saved` | Tokens not billed against the frontier model for this response |
| `tokens_saved_pct` | `tokens_saved` as a percentage of the relevant token baseline |

**Definitions (a proxy, not measured ground truth — same honesty caveat
as the rest of this MVP's economics, see Honest limitations below):**
- **Tool/local responses** (not escalated) never reached the frontier
  model at all, so the *entire* frontier-equivalent cost for the same
  token volume — estimated via the registry's frontier
  `cost_per_1k_tokens` rate — counts as saved. A tool-tier response with
  no tokens (e.g. `git status`) reports 0% since there's no token volume
  to compare against.
- **Frontier responses** (direct or escalated) already paid frontier
  cost — the only savings available there come from the Context Firewall
  (sections 12/23) shrinking the prompt before it was sent. Savings are
  computed from `context_tokens_before`/`context_tokens_after` when
  present, and zero otherwise.

**Where to see it:**
- `POST /execute` returns `baseline_cost`, `cost_saved`, `cost_saved_pct`,
  `tokens_saved`, `tokens_saved_pct` alongside the existing `cost` field.
- `GET /metrics` (the cheap rollup) adds `total_baseline_cost`,
  `total_cost_saved`, `cost_saved_pct`, `total_tokens_saved`.
- `GET /metrics/full` adds the same four fields computed across the full
  trace history via `alr/evaluation.py::compute_evaluation_metrics`.
- The trace store (SQLite and Postgres) persists all five per-response
  fields in new `baseline_cost`/`cost_saved`/`cost_saved_pct`/
  `tokens_saved`/`tokens_saved_pct` columns, migrated in place via
  `ALTER TABLE ... ADD COLUMN` so existing trace databases don't need to
  be recreated.

## Per-caller usage tracking

If you're running one deployment that multiple people/services call
(a shared team API), each caller can get their own named API key instead
of everyone sharing one anonymous secret — and their usage gets tracked
separately in your own trace store.

**Set up labeled keys** — `ALR_API_KEYS` accepts an optional
`key:identity` label per entry, comma-separated:

```bash
ALR_API_KEYS=sk-alice-xyz:alice,sk-bob-abc:bob,sk-team-ci:ci-pipeline
```

A plain key with no `:label` still works exactly as before (its identity
just defaults to the key itself) — this is fully backward compatible with
existing `ALR_API_KEYS=key1,key2` configs. The label is never a secret —
pick a name, a GitHub username, a team name — since it ends up in trace
data and API responses; the raw key itself is never logged or returned.

**Where the identity shows up:**
- `POST /execute` returns a `caller_id` field alongside the result.
- `GET /metrics` and `GET /metrics/full` both add `usage_by_caller` — a
  `{identity: {requests, cost, cost_saved}}` breakdown, e.g.:
  ```json
  {"usage_by_caller": {"alice": {"requests": 12, "cost": 0.081, "cost_saved": 0.24}}}
  ```
- The trace store persists a `caller_id` column per row (SQLite and
  Postgres, migrated in place) — `"anonymous"` for unauthenticated
  traffic or an unlabeled key, matching `alr/auth.py::resolve_caller_id`.

This only tracks usage on infrastructure *you* control (your own trace
store) — it has nothing to do with, and never touches, anyone's GitHub
account. If you want callers to authenticate with something like a real
GitHub identity rather than a static labeled key, swap `alr/auth.py` for
a real OAuth/JWT verifier that resolves `caller_id` from the token —
`resolve_caller_id()` is the one function everything downstream depends
on, so nothing else needs to change.

## Scheduled savings benchmark

`.github/workflows/benchmark.yml` runs every 6 hours (`cron: "0 */6 * *
*"`, plus a manual `workflow_dispatch` trigger) and:

1. Picks an auth method — `CLAUDE_CODE_OAUTH_TOKEN` (headless CLI,
   billed against your Claude Pro/Max subscription) if set, else
   `ANTHROPIC_API_KEY` (raw API billing) if that's set instead, else
   fails with a clear error naming both options.
2. Runs `benchmarks/record_savings.py` — a small, fixed task set (one
   free Tier-0 tool task, one task engineered to route straight to the
   frontier tier) through the real `Pipeline`.
3. Appends the run's per-task and aggregate savings numbers as one JSON
   line to `benchmarks/history/savings_history.jsonl`, and one row to the
   human-readable `benchmarks/history/SUMMARY.md` table.
4. Commits both files back to the repo (`[skip ci]` so it doesn't
   re-trigger the main test workflow) — so the savings track record is
   visible directly in git history, not just in a dashboard that resets.

**Setup required (pick one):**
- **Recommended — use your existing Claude Pro/Max subscription, no API
  key:** run `claude setup-token` once on any machine with the CLI
  installed and logged in. It prints a long-lived token — add it as the
  `CLAUDE_CODE_OAUTH_TOKEN` repository secret (Settings → Secrets and
  variables → Actions). The workflow installs the CLI itself
  (`npm install -g @anthropic-ai/claude-code`) and this token
  authenticates it headlessly, billed the same as your normal Claude Code
  usage.
- **Optional alternative — raw API billing:** add an `ANTHROPIC_API_KEY`
  repository secret instead. Only needed if you specifically want
  per-token API billing rather than subscription billing.

Neither secret is required for the rest of the app to work — this is only
for the scheduled workflow. It fails loudly with a clear error if neither
is configured, rather than silently no-opping.

**Before any of this works, the workflow file has to actually be on
GitHub** — cloning/editing this repo locally doesn't register the
workflow by itself. Commit `.github/workflows/benchmark.yml` (and
whatever else you've changed) and push to your default branch, or it
won't show up under the repo's **Actions** tab and setting the secret
beforehand accomplishes nothing yet.

**If the workflow runs but fails on the final `git push` step:** check
Settings → Actions → General → **Workflow permissions** on the repo. If
it's set to "Read repository contents" (read-only), the `permissions:
contents: write` declared in `benchmark.yml` usually overrides that for
its own run, but some org-level policies enforce read-only regardless —
switch it to "Read and write permissions" if the push step fails with a
403.

**Cost note:** this calls Claude 4 times a day, forever, for as long as
the workflow is enabled — against your subscription's usage limits if
using `CLAUDE_CODE_OAUTH_TOKEN`, or as metered spend if using
`ANTHROPIC_API_KEY`. The default task set is deliberately tiny and cheap
(one free tool call + one short architecture prompt) — if you expand
`TASKS` in `benchmarks/record_savings.py`, that cost scales with it.
Disable the workflow (or widen the cron interval) if you don't want the
recurring usage.

## Honest limitations (read before you pitch this to anyone)

- **The confidence signal is heuristic, not calibrated.** It blends a
  static registry prior, the local model's own self-report (which LLMs are
  notoriously bad at, self-reported numbers are not real calibration), and
  regex hedge-detection. This is explicitly the doc's own "Failure 1 — Local
  model overconfidence" risk (section 22) and it is **not solved** here —
  only mitigated. For real deployments look at self-consistency voting
  (sample N times, escalate on disagreement) before trusting this in
  production; see the `askalf/hybrid` project referenced earlier for a
  from-scratch example of that approach.
- **The task classifier is keyword-based**, exactly as the doc recommends
  for an MVP (section 9, Strategy A). It will misclassify tasks that don't
  use your keyword vocabulary. This is Phase 1 by design — Phase 3 in the
  doc's own roadmap replaces this with a learned classifier once you have
  trace data to train on (which `tracing.py` is already collecting).
- **The capability matrix numbers are made up.** They're structurally
  correct (local model weaker on architecture/debugging, roughly at parity
  on extraction/summarization) but not measured. Replace with real
  outcome-derived numbers once you have production traces — that's the
  entire point of `TraceStore.summary_metrics()` existing.
- **Cost tracking only covers frontier API cost**, not local inference's
  real effective cost (GPU/electricity/maintenance) that section 19 flags —
  add that yourself once you know your actual local infra costs.
- **The context firewall is intentionally naive** (regex error-line
  extraction + truncation), not the local-LLM-powered summarization the doc
  envisions in section 12. It's structured so you can swap in a real
  summarization call later without changing the `Evidence` interface.
- **No policy-approval UI.** `PolicyEngine.approval_callback` is a hook,
  not a workflow — wiring it to Slack/a queue/a human clicking a button is
  on you. Out of the box, anything requiring approval is denied.
- **False-local/false-frontier rate are proxies, not ground truth**
  (exactly as the doc's own section 15 warns). `false_local_rate` uses
  direct evidence (the local result failed or was escalated). Without
  human-labeled outcomes, `false_frontier_rate` is estimated from the
  model registry's capability-matrix priors and is `None` unless you pass
  a `ModelRegistry` into `compute_evaluation_metrics()` — see Evaluation
  Metrics below.
- **Context compression ratio and per-tier token counts are opt-in.**
  The context firewall only runs when a caller attaches raw evidence via
  `payload["context_text"]`; token counts use a word-count proxy, not a
  real tokenizer. Local-tier token counts depend on Ollama reporting
  `prompt_eval_count`/`eval_count` — not guaranteed for every
  Ollama-compatible backend.
- **`pass_at_k`/`pass_at_k_from_attempts` need an external benchmark
  harness**, not just production traces — they require N sampled
  attempts per task (section 35's benchmark design), which a live
  single-shot production trace stream doesn't have by construction.
- **The Kubernetes manifests (`k8s/`) are standard-pattern and
  YAML-validated but not applied to a live cluster** in this environment
  (no kubectl/kind/minikube available) — `kubectl apply --dry-run=client
  -f k8s/` in your own cluster context before relying on them. The
  docker-compose multi-replica path *was* verified end-to-end with real
  containers — see Autoscaling below.

## What changed to close the deployment gaps

| Gap (from the earlier review) | What was added |
|---|---|
| No auth | `alr/auth.py` — API-key header check via `ALR_API_KEYS` env var, constant-time comparison, all three data endpoints gated |
| No policy/approval layer | `alr/policy.py` — risk-pattern matching + hard-deny/require-approval, enforced *inside* `Pipeline` so `/execute` can't bypass it |
| No rate limiting | `alr/rate_limit.py` — per-key token bucket, tunable via `ALR_RATE_LIMIT_*` env vars |
| No request size limits | `alr/api.py` middleware rejects bodies over `ALR_MAX_BODY_BYTES` (default 200KB) before they're parsed |
| Blocking calls inside async routes | `alr/local_llm.py` / `alr/frontier_llm.py` gained `call_*_async()` using `asyncio.to_thread`; `Pipeline.run_async()` is what `/execute` calls |
| No retries / circuit breaker | `alr/retry.py` — exponential backoff + a circuit breaker so one dead endpoint fails fast instead of hanging every request |
| Path traversal in Tier-0 tools | `alr/tools.py::_safe_path()` resolves and rejects any path outside `ALR_WORKSPACE_ROOT`; `test_execution` now uses a command allowlist + `shlex`, not a raw shell string |
| SQLite locks under concurrency | WAL mode + busy_timeout in `alr/tracing.py`; `PostgresTraceStore` provided (see limitations above) for multi-replica setups |
| No structured logging | `alr/logging_config.py` — JSON lines to stdout, `trace_id` attached where available |
| No Dockerfile | `Dockerfile` — non-root user, healthcheck, multi-stage-ready |
| No health/readiness split | `/health` (liveness) vs `/ready` (actually checks the trace store) |
| No CI | `.github/workflows/ci.yml` — runs the full test suite + a Docker build on every push |
| No graceful startup/shutdown | FastAPI `lifespan` context in `api.py` |

99 tests total (51 from the core/pipeline/deployment-readiness suites
(includes the per-caller auth identity tests) + 19 in
`tests/test_evaluation.py` + 9 in `tests/test_scaling_infra.py` + 15 in
`tests/test_frontier_transport.py` (CLI/API dispatch, the CLI
stdout-error-reporting regression, and frontier-auth startup validation)
+ 5 in `tests/test_savings.py`), all passing on stdlib + PyYAML — run
them yourself: `python3 -m unittest discover -s tests -v`.

## What changed to make it autoscalable and add the full evaluation framework

| Gap | What was added | Bugs found & fixed along the way |
|---|---|---|
| Rate limiting was per-process only | `alr/rate_limit.py::RedisRateLimiter` — Lua-scripted atomic token bucket in Redis; `build_rate_limiter()` factory picks it when `REDIS_URL` is reachable, else falls back to in-memory | — |
| Trace store didn't support multiple replicas safely | `PostgresTraceStore` verified against a live `postgres:16-alpine` instance, including schema migration for new columns | **Two real bugs**: (1) psycopg sends Python `bool` as Postgres `boolean` but the `success`/`escalated` columns are `INTEGER` — fixed by casting to `int()` before the insert, matching what the SQLite path already did. (2) `CREATE TABLE IF NOT EXISTS` races under concurrent replica startup (`UniqueViolation` on `pg_type_typname_nsp_index`) — fixed with a `pg_advisory_lock` around schema setup. Reproduced with 3 real concurrent Docker containers, see Autoscaling section |
| Full metric set from sections 16-18 didn't exist | `alr/evaluation.py` — `compute_evaluation_metrics()`, `pass_at_k`/`pass_at_k_from_attempts`, `run_baseline`/`compare_baselines` (section 35); `GET /metrics/full` | — |
| No context-token/model-token tracking | `ExecutionResult` gained `input_tokens`/`output_tokens`/`context_tokens_before`/`context_tokens_after`; threaded through both sync and async execution paths | Ollama's `prompt_eval_count`/`eval_count` weren't being read at all before this |
| Context Firewall (sections 12/23) was never actually called by the pipeline | `Pipeline._build_frontier_prompt()` compresses `payload["context_text"]` via `ContextManager` before it reaches the frontier model, whenever a caller attaches raw evidence | — |
| `ModelSpec` silently dropped `endpoint` from `models.yaml` | `alr/registry.py` now reads and stores it | **Real bug**: every Tier-1 call crashed with `AttributeError: 'ModelSpec' object has no attribute 'endpoint'` instead of failing closed with `LocalLLMUnavailable` as the README claimed. Found by actually running `pipeline_demo.py`, not just reading the code |
| No Kubernetes path | `k8s/` — Deployment (resource requests for HPA, readiness/liveness probes, PDB), HPA (CPU/memory, tuned scale-down stabilization), ConfigMap, Secret example, demo Postgres/Redis | — |
| docker-compose didn't scale | Added Postgres + Redis + nginx (round-robin via Docker's embedded DNS resolver) as default services; `alr` has no fixed host port so `--scale alr=N` works without port collisions | — |

All of the above — Redis-backed rate limiting shared across replicas,
Postgres-backed tracing shared across replicas, the concurrent-startup
race and its fix — were verified against real Docker containers during
development, not just written and assumed correct. See the Autoscaling
section for exactly what was tested and how to reproduce it.

## What changed to add dual transports and savings tracking

| Gap | What was added |
|---|---|
| Frontier connector only worked with an interactive `claude login` session, no headless option | `CLAUDE_CODE_OAUTH_TOKEN` support for the existing CLI transport (headless, still billed against your Claude Pro/Max subscription — no code change needed, `subprocess.run()` already inherits the environment) plus a fully optional `alr/frontier_llm.py::_call_frontier_llm_api` — raw Anthropic Messages API over stdlib `urllib`, selected via `ALR_FRONTIER_TRANSPORT=api` — see "Two ways to run Tier 2" |
| No per-response savings figure — only aggregate benchmark comparisons | `Pipeline._compute_savings()` populates `baseline_cost`/`cost_saved`/`cost_saved_pct`/`tokens_saved`/`tokens_saved_pct` on every `ExecutionResult`, surfaced in `/execute`, `/metrics`, `/metrics/full`, and persisted in the trace store (`_V3_COLUMNS` in `alr/tracing.py`, migrated in place) — see "Token & cost savings tracking" |
| No automated record of savings over time | `.github/workflows/benchmark.yml` — 6-hour cron running `benchmarks/record_savings.py` against the API transport, committing results to `benchmarks/history/` — see "Scheduled savings benchmark" |

## What changed to add per-caller usage tracking and startup auth enforcement

| Gap | What was added |
|---|---|
| `ALR_API_KEYS` validated a caller but never identified *which* caller — usage wasn't attributable per key at all | `alr/auth.py::load_api_keys_from_env` now accepts optional `key:identity` labels; `resolve_caller_id()` returns the label (never the raw secret key) to `/execute`/`/route`; threaded through `TaskEnvelope.caller_id` → `Pipeline` → `TraceStore` (new `caller_id` column, `_V4_COLUMNS`, migrated in place) → `usage_by_caller` in `GET /metrics` and `/metrics/full` — see "Per-caller usage tracking" |
| A missing/invalid frontier credential only surfaced as a confusing per-request failure (exactly what happened during development — see the CLI stdout-parsing bug fixed in `_call_frontier_llm_cli`) | `alr/frontier_llm.py::validate_frontier_auth()` runs at API startup and refuses to serve traffic if a cloud-capable model is registered but no usable credential is configured for its transport — skips the check entirely for local/tool-only deployments with no cloud model registered |

## Deploying it

> **Note:** the steps below build a container image, and this Dockerfile
> doesn't install the `claude` CLI — so the simplest path here is
> `ALR_FRONTIER_TRANSPORT=api` with `ANTHROPIC_API_KEY` in `.env` (see
> "Two ways to run Tier 2" above). If you'd rather keep billing against
> your Claude Pro/Max subscription instead of the API, add Node.js + `npm
> install -g @anthropic-ai/claude-code` to the Dockerfile and set
> `CLAUDE_CODE_OAUTH_TOKEN` instead — that's exactly what
> `.github/workflows/benchmark.yml` does for the scheduled benchmark, just
> not wired into this Dockerfile by default. Running the app directly on
> your own machine (Quick start) can leave `ALR_FRONTIER_TRANSPORT` unset
> to use the CLI-based connector with your regular `claude login` session,
> no API key at all.
>
> **The API will now refuse to start at all** if it has a cloud-capable
> model registered (the default `models.yaml` always does) but no usable
> Claude credential configured for the resolved transport —
> `alr/frontier_llm.py::validate_frontier_auth()` runs at startup and
> raises before the app accepts any traffic, rather than letting a
> half-configured deployment fail confusingly on its first real request.
> Each deployer needs their own credential; none ship with this repo.

```bash
# 1. Configure
cp .env.example .env
# edit .env: set ALR_API_KEYS (required), and for this container image
# also set ALR_FRONTIER_TRANSPORT=api + ANTHROPIC_API_KEY (optional —
# see the note above for the subscription-billed alternative)

# 2. Build + run
docker compose up --build

# 3. Verify
curl localhost:8000/health
curl localhost:8000/ready
curl -X POST localhost:8000/execute \
  -H "x-api-key: $ALR_API_KEYS" -H "Content-Type: application/json" \
  -d '{"task": "git status"}'
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ALR_API_KEYS` | unset (auth OFF) | Comma-separated valid API keys, each optionally labeled `key:identity` (e.g. `key1:alice,key2:bob`) for per-caller usage tracking — see "Per-caller usage tracking". **Set this in any real deployment** — startup logs a warning if unset. |
| `ALR_FRONTIER_TRANSPORT` | `cli` | `cli` (shells out to `claude`, billed against your subscription) or `api` (raw Anthropic Messages API, optional, billed as API usage) — see "Two ways to run Tier 2". Overridable per model via `transport:` in `models.yaml`. |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | Optional — headless auth for the default `cli` transport (from `claude setup-token`), billed against your Claude Pro/Max subscription. Not needed if you have an interactive `claude login` session instead. |
| `ANTHROPIC_API_KEY` | unset | Optional — only used when `ALR_FRONTIER_TRANSPORT=api`. Not required for the default `cli` transport, interactive or headless. |
| `DATABASE_URL` | unset (uses SQLite) | Postgres DSN — switches `build_trace_store()` to `PostgresTraceStore` (see limitations above). A free-tier Neon database works here with zero code changes — see "Cloud trace store (free tier)". |
| `ALR_WORKSPACE_ROOT` | `.` | Root that Tier-0 filesystem tools (git/grep/ls) are sandboxed to. |
| `ALR_MAX_BODY_BYTES` | `200000` | Max request body size. |
| `ALR_RATE_LIMIT_CAPACITY` | `60` | Token bucket burst capacity per API key. |
| `ALR_RATE_LIMIT_REFILL_PER_S` | `1.0` | Steady-state requests/sec per API key. |
| `REDIS_URL` | unset (in-memory limiter) | Redis DSN — switches `build_rate_limiter()` to `RedisRateLimiter` so rate limits are shared across replicas. Falls back to in-memory (with a startup warning) if unset or unreachable. |

## Cloud trace store (free tier)

By default the trace store (every task, cost, savings figure, per-caller
usage row) lives in a local SQLite file (`alr_traces.db`) — fine for a
single machine, but it disappears if that disk does, and it isn't
visible from anywhere else. `alr/tracing.py::build_trace_store()` already
switches to `PostgresTraceStore` the moment `DATABASE_URL` is set, so
pointing it at a cloud Postgres instance needs **no code change** — just
the connection string.

**[Neon](https://neon.com)** has a permanent free tier (not a time-limited
trial) that fits this: 0.5GB storage, 100 compute-hours/month, no credit
card required, and it scales to zero when idle (so a low-traffic personal
deployment effectively costs nothing).

**Setup:**
1. Sign up at neon.com, create a project, and copy its connection string
   (`postgresql://<user>:<password>@<host>/<dbname>?sslmode=require`).
2. **Never paste that string into a chat or commit it to git** — it
   contains a real password, the same sensitivity class as an API key.
   Set it as an environment variable directly in your own terminal or
   `.env` file instead:
   ```bash
   export DATABASE_URL="postgresql://...your-neon-connection-string..."
   ```
3. Run the app as usual (`uvicorn alr.api:app` or `python3
   pipeline_demo.py`) — `psycopg[binary]` is already in
   `requirements.txt`, so no extra install is needed. `PostgresTraceStore`
   handles schema setup itself on first connect (including the
   concurrent-startup-safe advisory lock described in Autoscaling below).
4. Verify it's actually using Postgres, not silently falling back to
   SQLite: hit `GET /ready` (fails loudly if the configured store is
   unreachable) or check the trace data shows up in Neon's own SQL editor
   after running a task.

**Trade-off:** free-tier compute-hours and idle-scale-to-zero mean the
*first* request after a period of inactivity pays a cold-start penalty
(typically a few hundred ms to a couple of seconds) while Neon wakes the
database back up — negligible for personal use, worth knowing about
before you assume every request has the same latency.

## Autoscaling

The API tier (`alr/api.py`) is stateless by design — every piece of
per-request state a horizontally-scaled deployment needs to share lives
outside the process:

| State | Single replica (default) | Multi-replica (autoscaled) |
|---|---|---|
| Trace store | SQLite file (`alr_traces.db`) | Postgres — set `DATABASE_URL` |
| Rate limiter | In-memory token bucket | Redis — set `REDIS_URL` |
| Router, analyzer, validator, policy engine, connectors | stateless per-request either way | stateless per-request either way |

Set both env vars and the app is safe to run as N replicas behind any
load balancer — nothing else changes.

**This was verified, not just asserted.** During development this repo's
own `alr:latest` image was run as 3 independent Docker containers on a
shared Docker network, pointed at one Postgres and one Redis container,
with no host-level coordination between them:

- **Distributed rate limiting**: 65 requests fired at replica 1 with the
  same API key returned exactly 60×`200` then 5×`429` (the default
  capacity) — then a *single* request to replica 2 and to replica 3, with
  the same key, immediately returned `429` too. The bucket was shared
  correctly, not per-process.
- **Shared trace store**: one `/execute` request sent to each of the 3
  replicas landed all 3 rows in the one Postgres `traces` table;
  `GET /metrics` from any replica reported `total_requests: 3` regardless
  of which replica actually served each request.
- **A real bug was found and fixed in the process**: launching 3
  replicas *simultaneously* (not staggered) reproducibly crashed one of
  them with `psycopg.errors.UniqueViolation` on
  `pg_type_typname_nsp_index` — Postgres's `CREATE TABLE IF NOT EXISTS`
  is not atomic across concurrent sessions, so N replicas booting at once
  can all pass the "does it exist" check and race on creating it. Fixed
  in `PostgresTraceStore.__init__` (`alr/tracing.py`) with a Postgres
  advisory lock (`pg_advisory_lock`) that serializes schema setup across
  replicas — the first replica does the DDL, the rest block briefly and
  then see the table already exists. Covered by a regression test
  (`tests/test_scaling_infra.py::test_concurrent_schema_setup_does_not_race`)
  that reproduces the race with 8 concurrent threads against a live
  Postgres instance.

### Testing the autoscaling path yourself

```bash
# Bring up Postgres + Redis (or use docker-compose.yml's services directly)
docker run -d --name alr-pg -p 5433:5432 \
  -e POSTGRES_USER=alr -e POSTGRES_PASSWORD=alr -e POSTGRES_DB=alr postgres:16-alpine
docker run -d --name alr-redis -p 6379:6379 redis:7-alpine

# Run the autoscaling-specific tests against them (they auto-skip if
# unreachable, so this is safe to run even without Docker):
TEST_POSTGRES_DSN=postgresql://alr:alr@localhost:5433/alr \
TEST_REDIS_URL=redis://localhost:6379/0 \
python3 -m unittest tests.test_scaling_infra -v
```

### docker-compose: multi-replica locally

`docker-compose.yml` now runs Postgres + Redis + an nginx load balancer
by default, with the `alr` service pointed at both — it's already
scale-ready:

```bash
docker compose up -d --build --scale alr=3
curl localhost:8000/health   # -> {"status":"ok","instance":"<container-hostname>"}
```

Hit `/health` repeatedly and watch the `instance` field change — that's
nginx (`nginx.conf`, using Docker's embedded DNS resolver so it
re-resolves the `alr` service name on every request instead of pinning to
whichever replica answered first) round-robining across your 3 replicas.

### Kubernetes: HPA-based autoscaling

`k8s/` has a complete manifest set: `deployment.yaml` (Deployment +
Service + PodDisruptionBudget, CPU/memory resource requests set — HPA
needs those to compute utilization), `hpa.yaml` (HorizontalPodAutoscaler,
CPU/memory-based, `minReplicas: 2` / `maxReplicas: 10`, tuned scale-down
stabilization to avoid flapping), `configmap.yaml` (the model registry,
editable without a rebuild), `secret.example.yaml` (copy and fill in real
values — never apply the example as-is), and `postgres.yaml`/`redis.yaml`
(single-instance demo backends — point at managed Postgres/Redis instead
for real production use).

```bash
kubectl apply -f k8s/configmap.yaml -f k8s/secret.yaml   # your filled-in copy
kubectl apply -f k8s/postgres.yaml -f k8s/redis.yaml
kubectl apply -f k8s/deployment.yaml -f k8s/hpa.yaml
kubectl get hpa alr-hpa --watch
```

The HPA scales on CPU/memory by default (universally available); the
commented block at the bottom of `hpa.yaml` shows how to scale on an
app-level metric (e.g. requests-in-flight) via the Prometheus Adapter or
KEDA instead, using `GET /metrics/full` as the data source.

## Suggested next steps (mapped to the doc's own roadmap, section 40)

1. Point the local connector at a real Ollama instance and run
   `pipeline_demo.py` against real tasks from your own repo.
2. Build and run the Docker image yourself and confirm the healthcheck,
   auth, and rate limiting behave as expected against real traffic —
   these are written and unit-tested but not integration-tested against
   a live container in this environment.
3. Once you have a few hundred traces in `alr_traces.db`, inspect
   `escalation_rate` and `task_success_rate` from `/metrics` — that's your
   false-local rate proxy (section 18).
4. Replace the capability-matrix priors in `models.yaml` with numbers
   derived from those traces.
5. Wire `PolicyEngine.approval_callback` to a real approval workflow
   before enabling any high-risk task categories in production.
6. Only then consider Strategy B (learned classifier) — the doc explicitly
   warns against building this before you have real routing data.
