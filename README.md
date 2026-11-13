# RUPIKA — Rupa, Pikiran, dan Karya

Telegram-controlled, 24/7 AI stock-asset production pipeline. Python CLI + SQLite, deployed as a dedicated Docker Compose project on a VPS that also hosts production applications.

> **Status:** implementation specification, not completed software. Antigravity must implement and test all acceptance criteria before deployment. No automatic Adobe Stock submission.

## Goals
- Research commercial image niches and generate distinct concepts.
- Route **research, prompt, image, vision QC, metadata** to independently configured API providers/models.
- Generate images only after explicit admin approval of an estimated spend.
- Perform deterministic QC, similarity detection, optional vision QC, and human review via Telegram.
- Export reviewed JPEG files and Adobe-compatible metadata CSV; upload and AI disclosure remain manual in Adobe Contributor Portal.
- Track job history, costs, compliance evidence, and moderation outcomes.

## MVP non-goals
No web dashboard, multi-tenant billing, browser automation of Adobe, scraping against site terms, local GPU inference, public HTTP listener, or unattended auto-generation.

## Runtime architecture
- `stockforge bot`: Telegram long polling, private-chat whitelist, commands, inline review.
- `stockforge worker`: SQLite job leasing, provider calls, QC, metadata, export.
- `stockforge scheduler`: daily backups, disk checks, health summaries.
- SQLite WAL and asset files in a persistent Docker named volume mounted at `/data`; source and Compose project under `/opt/stockforge`.

## Stack
Python 3.12+, Typer, python-telegram-bot, SQLAlchemy, Alembic, Pydantic Settings, httpx, Pillow, imagehash, optional OpenCV, pytest. Prefer standard library where practical.

## Project tree
```text
stockforge/
  README.md AGENT.md ARCHITECTURE.md KANBAN.md BACKLOG.md TASK.md
  pyproject.toml .env.example .gitignore alembic.ini
  config/{models.yaml,production.yaml,compliance.yaml}
  src/stockforge/
    {cli.py,config.py,database.py}
    telegram/{app.py,auth.py,keyboards.py,callbacks.py,handlers/}
    ai/{router.py,registry.py,providers/}
    core/{orchestrator.py,job_queue.py,budget.py,qc.py,compliance.py,exporter.py}
    db/{models.py,repositories.py}
    workers/{main.py,recovery.py}
    scheduler/{main.py,backup.py}
  migrations/ tests/{unit,integration,fixtures}/
  Dockerfile compose.yaml .dockerignore
  deploy/{backup.sh,restore.sh,rollback.md}
  data/  # gitignored; local development only
```

## CLI contract (implement)
```bash
stockforge init
stockforge doctor
stockforge bot
stockforge worker
stockforge scheduler
stockforge niche add cybersecurity
stockforge research --niche cybersecurity --limit 10
stockforge jobs list
stockforge qc run --pending
stockforge export adobe --approved
stockforge stats
```

## Telegram command contract
`/start`, `/help`, `/research`, `/ideas`, `/generate`, `/jobs`, `/status <batch>`, `/failed <batch>`, `/retry <batch>`, `/pause`, `/resume`, `/cancel`, `/review`, `/export`, `/stats`, `/cost`, `/models`, `/providers`, `/settings`, `/health`. Private chats and approved numeric user IDs only. Never accept API credentials in chat.

## Configuration
See `.env.example` in TASK.md. API roles have separate URLs, keys, and model IDs; image API adapter is **not assumed** to be OpenAI-chat-compatible. Validate actual provider schemas, pricing, commercial licensing, and sublicensing permission before enabling production.

## Development sequence
Follow TASK.md tasks in order; move cards in KANBAN.md only after passing tests. Start with one vertical slice: one approved concept → one image → QC → metadata → manual review → CSV/JPEG export. Then add batch processing and Telegram workflows.

## Production safety
Do not touch existing Nginx, PostgreSQL, Hermes, or production service units. Use separate Docker Compose project, non-root container UID, CPU/memory/PID limits, no published ports, outbound-only API calls. Take a VPS backup/snapshot before deployment; test on staging paths and activate one service at a time.

## Adobe policy
Generative AI content must be appropriately labeled, licensed for stock redistribution, non-infringing, technically acceptable, and not repetitive. Human must verify official current Contributor guidelines, image specs, CSV template and any release/disclosure fields at submission time. Never treat model-generated compliance scores as proof of legal clearance.


## v1.1 mandatory additions: batch observability and resilience
- Each image is an independently tracked `job_item`: index/total, pipeline stage, attempt, provider/model, success/failure, timestamps, estimated and actual spend.
- Telegram posts one progress message per batch and edits it on stage transitions or item completion (throttle to >=5 seconds where practical). Show completed/failed/processing/pending counts, **image N of M**, current stage, provider, and cost. Never misrepresent `100% processed` as `100% successful`.
- Normalize provider-specific errors to precise codes (`INSUFFICIENT_CREDITS`, `TOKEN_QUOTA_EXCEEDED`, `RATE_LIMITED`, etc.) using response body + headers + provider adapter; HTTP status alone is insufficient. Notify admin of cause, image index, retry/fallback action and outcome.
- Bounded retries and compatible, licensed fallback provider routes; no fallback for policy refusal, internal budget exceeded, or unsafe unknown-cost retries. Timeout after possibly billable request becomes `needs_reconciliation` unless provider supports idempotent recovery.
- All state, attempts, error classifications, Telegram progress message IDs and notification events persist in SQLite for restart recovery.
- Adobe upload and generative-AI disclosure remain **manual**. Bot sends previews for review and approved ZIP (full-resolution JPEG + CSV + manifest); large ZIPs require secure download delivery, not public storage.

## Docker deployment contract
- Compose project name `stockforge`; services `bot`, `worker`, `scheduler` share an image and named volume `stockforge_data:/data`; one private Compose network, **no `ports:`**.
- SQLite path `sqlite:////data/stockforge.db`; directories `/data/raw`, `/data/processed`, `/data/thumbnails`, `/data/exports`, `/data/backups`.
- Run container as non-root UID 10001, initialize named-volume ownership safely, mount config read-only, restrict capabilities and privileges, set resource limits, and avoid host Docker socket mounts.
- Do not use `docker compose down -v`; image/container recreation must retain database and assets. Docker volumes are **not** off-host backups. Back up SQLite with its online backup API and important assets to a separate destination; test restore.
- Before deploying to the shared VPS, check actual free RAM/disk and production workloads; proposed limits are starting points, not guaranteed safe. No modifications to website, Nginx, PostgreSQL or other Compose projects.

## Quick operator workflow
`/generate` → confirm budget → `/status SF-001` → progress notifications/fallback alerts → preview and `/review` → `/export` → download JPEG/CSV ZIP → manually upload and disclose AI in Adobe Contributor. `/failed` and `/retry` handle individual failures without repeating successful items.

## v1.2 — Research modes and human approval (P0)
StockForge supports exactly two research modes, configured per installation and changeable by authorized Telegram admin:

| Mode | Trigger | What happens | Required operator decision |
|---|---|---|---|
| `manual` (default) | `/research` | AI researches niches, saves source-aware suggestions and sends Telegram choices | Operator selects/rejects niche(s), reviews plan, explicitly approves paid generation |
| `auto` | Scheduler at configured timezone/cron | AI runs research within its own spend limits, deduplicates findings and sends recommendations | **Same niche selection and production approval gates**; no unattended image generation |

Research may itself incur paid LLM/search costs. Both modes require preconfigured research spending limits; auto runs must stop at cap, and initial enabling of paid scheduled research requires admin confirmation. Never confuse permission to spend on research with permission to generate images. `auto_generate=false` is a hard invariant; no scheduler, retry, fallback, callback, CLI, or restart path may bypass the approval gate.

**User journey:** `/settings` → Research Mode → `Manual` / `Auto`; `/research [topic]` → research report with sourced niche candidates and clearly labelled heuristic scores → `Select Niche` (multi-select), `Research Again`, `Reject` → proposed concept count, image count, provider/model, maximum cost and license warnings → `Approve Production` (explicit confirmation with plan fingerprint and expiry) → queue image generation → progress/QC → asset review → export → manual Adobe submission. An edited or expired plan requires fresh approval. If research has no credible external data, show *AI hypothesis, not validated market demand* rather than fabricated demand/competition metrics.

**Auto mode:** `research.mode: auto`, `research.auto.enabled: true`, cron/timezone `Asia/Jakarta`, bounded `max_niches`, no overlapping scheduled runs, and a notification digest with inline selection. Manual `/research` still works while auto is enabled. Switching to manual cancels future scheduled research without deleting history or cancelling already approved image jobs. Defaults: `manual`, `auto.enabled=false`, `auto_generate=false`.

**Workflow states:** `RESEARCHING → AWAITING_NICHE_SELECTION → PLANNING → AWAITING_GENERATION_APPROVAL → GENERATING → REVIEW → EXPORT_READY`; allow `RESEARCH_FAILED`, `REJECTED`, `EXPIRED`, `CANCELLED`. Research completion must **never** enqueue an image-generation job directly. See ARCHITECTURE.md and SF-020–SF-022 in TASK.md.

## v1.3 — Mandatory ETA and progress reporting

Two distinct ETA features are required:

1. **Antigravity implementation-task ETA (agent workflow):** Before starting any `SF-###` task, Antigravity must announce its scope, dependency checks, estimated active work duration as a range, start time with timezone if available, tentative finish time if a reliable clock is available, confidence (low/medium/high), assumptions, and key risks. It must report progress after meaningful milestones, explain blockers, and revise the ETA when new evidence changes the forecast. Do not invent elapsed time or imply background execution when the agent is not running.
2. **StockForge runtime job ETA (Telegram):** For research, prompt, generation, QC, metadata, and export jobs, show current stage, images completed/total, estimated remaining time as a range, confidence, and `ETA unavailable — collecting samples` when insufficient historical data exists. Record real stage timings and refresh ETA from observed durations. Never imply exact completion time or guarantee paid API latency.

**Safety:** ETA is advisory only. No autonomous paid generation, approval bypass, or deployment is authorized by an ETA. All existing human-approval gates remain in force. See `AGENT.md`, `ARCHITECTURE.md`, and SF-023 in `TASK.md`.
