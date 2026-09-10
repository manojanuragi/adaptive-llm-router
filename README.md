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
| Tier 2 frontier LLM connector (Anthropic) | ✅ real code, sync + async, retry/circuit-breaker; needs `ANTHROPIC_API_KEY` to execute |
| Confidence/escalation logic | ✅ real, tested (heuristic signals — see "Honest limitations") |
| Context firewall (compression + progressive disclosure) | ✅ real, tested, wired into frontier calls when `payload["context_text"]` is set |
| Policy engine (section 21) | ✅ real, tested — deny/require-approval on high-risk patterns, enforced inside the pipeline |
| Auth (API keys) | ✅ real, tested |
| Rate limiting | ✅ real, tested — **Redis-backed and verified across independently running containers** when `REDIS_URL` is set; falls back to in-memory (single-process) otherwise |
| Retry + circuit breaker | ✅ real, tested |
| Trace store + rollup metrics | ✅ real, tested — **Postgres backend verified against a live `postgres:16-alpine` instance**, including a concurrent-startup race that was found and fixed (see Autoscaling section); SQLite remains the single-replica default |
| Evaluation framework (sections 16, 17, 18) | ✅ real, tested — `alr/evaluation.py`: full metric set, pass@k, baseline comparison harness (see Evaluation Metrics section) |
| FastAPI HTTP layer (auth, rate limit, size limits, lifespan, health/ready) | ✅ real, tested against live HTTP traffic |
| Dockerfile + docker-compose + Kubernetes manifests + CI | ✅ **image built and run**; multi-replica scaling verified with real containers (see Autoscaling section); k8s manifests are standard-pattern and YAML-validated, not yet applied to a live cluster in this environment |
| Learned classifier / contextual bandit / RL (Strategies B–E) | ❌ not built — Phase 3+ per the doc's own roadmap |
| Model competition, router memory, dynamic model marketplace | ❌ not built — "Advanced Features" in the doc, intentionally deferred |
| Approval-workflow UI (Slack/queue/human-in-the-loop) | ❌ not built — `PolicyEngine.approval_callback` is a hook for you to wire up |

76 tests pass. The core 67 use only the Python standard library +
PyYAML (no network, no API keys, no local model server, no Docker
required). 9 more exercise the Redis/Postgres-backed autoscaling path
against real containers and auto-skip if those aren't reachable (see
"Testing the autoscaling path" below). Run them yourself:
`python3 -m unittest discover -s tests -v`.

## Quick start

```bash
# 1. Zero-dependency taste test — routing decisions only, no execution
pip install pyyaml   # if you don't have it
python3 demo.py

# 2. Run the test suite
python3 -m unittest discover -s tests -v

# 3. Real execution (needs Ollama and/or ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
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
  frontier_llm.py Tier 2 — Anthropic API connector, sync + async (stdlib urllib, no SDK dependency)
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
demo.py               Zero-dependency routing-decisions-only demo
pipeline_demo.py      Full pipeline demo with real execution
Dockerfile            Non-root, healthcheck, workspace-sandboxed
docker-compose.yml    App (N replicas) + nginx load balancer + Postgres + Redis + Ollama — scale-ready by default
nginx.conf            Round-robin load balancer config for docker-compose --scale
k8s/                  Kubernetes manifests: Deployment, Service, HPA, ConfigMap, PDB, demo Postgres/Redis
.github/workflows/ci.yml  Test suite + Docker build on every push
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

76 tests total (48 from the original core/pipeline/deployment-readiness
suites + 19 in `tests/test_evaluation.py` + 9 in
`tests/test_scaling_infra.py`), all passing on stdlib + PyYAML — run them
yourself: `python3 -m unittest discover -s tests -v`.

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

## Deploying it

```bash
# 1. Configure
cp .env.example .env
# edit .env: set ALR_API_KEYS (required), ANTHROPIC_API_KEY

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
| `ALR_API_KEYS` | unset (auth OFF) | Comma-separated valid API keys. **Set this in any real deployment** — startup logs a warning if unset. |
| `ANTHROPIC_API_KEY` | unset | Required for Tier-2 frontier execution. |
| `DATABASE_URL` | unset (uses SQLite) | Postgres DSN — switches `build_trace_store()` to `PostgresTraceStore` (see limitations above). |
| `ALR_WORKSPACE_ROOT` | `.` | Root that Tier-0 filesystem tools (git/grep/ls) are sandboxed to. |
| `ALR_MAX_BODY_BYTES` | `200000` | Max request body size. |
| `ALR_RATE_LIMIT_CAPACITY` | `60` | Token bucket burst capacity per API key. |
| `ALR_RATE_LIMIT_REFILL_PER_S` | `1.0` | Steady-state requests/sec per API key. |
| `REDIS_URL` | unset (in-memory limiter) | Redis DSN — switches `build_rate_limiter()` to `RedisRateLimiter` so rate limits are shared across replicas. Falls back to in-memory (with a startup warning) if unset or unreachable. |

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
