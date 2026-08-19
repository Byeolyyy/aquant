# aquant

aquant is a local-first Windows desktop app that turns a pasted PTrade quant report into an evidence-backed, auditable research brief. It pairs a deterministic outer harness (parsing, permissions, budgets, auditing, termination) with a dynamic inner layer of expert agents: a coordinator Agent picks the experts each report needs, dispatches them in parallel, reviews their work, and writes the final synthesis.

The repository currently contains a runnable, governed, auditable desktop vertical slice:

- Electron + React three-pane desktop UI with an original "ink-study" research-room design.
- Python JSONL sidecar with versioned IPC.
- Deterministic parsing of raw PTrade text, with a confirmation preview.
- 5 default agents: coordinator, quant signal, company & industry, global markets, risk.
- Expert selection driven by the report's capabilities, not a fixed all-agents poll.
- The coordinator reviews expert output after parallel execution; on contradictions, material findings or evidence gaps it can re-invoke existing sub-agents with specific follow-up questions.
- All autonomous follow-ups are bounded by the existing agent allowlist, duplicate-task filtering and `max_rounds`; the model cannot invent new roles.
- The risk agent checks for potential negatives per ticker before the report date; the coordinator reviews again before the final synthesis.
- Pause, cancel, and node-boundary interruption protocol.
- SQLite persistence for reports, runs and ordered events.
- In-app run history: inspect past runs with timing/evidence/risk/model-call metrics, one-click rerun, Markdown export.
- In-app agent management: toggle optional agents, per-project extra requirements, config versioning; core governance roles cannot be disabled.
- In-app prompt workbench: view full system prompts, save drafts, publish, browse history, roll back; platform safety policies are read-only.
- Every run records the actual agent config, prompt versions and sub-workflow versions it used.
- Quant-signal demo sub-workflow: stable security IDs and name history, deterministic PTrade rules, per-run signal observations and stability statistics.
- Global-markets demo sub-workflow: anchored on the A-share report date — US indices take the latest trading day strictly before it, Korea and Japan take the latest trading day no later than it — with normalized moves and a five-day line chart. Yahoo Finance is the primary source; when it is unreachable the client automatically switches to mirror sources (Tencent for US indices, Eastmoney for Korea and Japan, Sina as a further Nikkei backup) and clearly labels the data as delayed quotes.
- Agent lifecycle and model-usage events: phase timing, evidence, risk, token, failure and fallback metrics.
- In-app "Connections & keys" settings hub: model, Tushare and Tavily can each be saved and connection-tested.
- The company & industry agent integrates Tushare, CNINFO announcements, Eastmoney news & research reports, plus optional Tavily for recent industry background.
- External material produces Evidence IDs, source links, summaries and retrieval times; the coordinator may only cite registered evidence.
- API keys are encrypted with Windows DPAPI (current-user scope); the UI and protocol only ever return "configured or not".
- Optional OpenAI-compatible coordinator model; without one, a testable local fallback keeps the product usable.
- Portable Windows build: the Python harness is frozen with PyInstaller and bundled by electron-builder into a folder that runs on a clean Windows machine with no Node/Python installed.

## Quick start

Requirements: Windows, Python 3.11+, Node.js 22+.

```powershell
cd E:\quant-agent
npm.cmd install
npm.cmd run setup:electron
npm.cmd run build
npm.cmd run dev
```

`setup:electron` downloads the Electron Windows runtime on first install; it is not needed for daily development.

## Portable build (zero-install)

To produce a portable folder that runs on a clean Windows machine without Node or Python:

```powershell
npm.cmd run package:win
```

Build flow: PyInstaller freezes the Python harness into `quant-agent-harness.exe` → it is copied into `apps/desktop/resources/harness/` → electron-builder packages it into the portable folder `apps/desktop/release/win-unpacked/` (with a `使用说明.txt` usage note) and produces the `artifacts/aquant-portable-win-x64.zip` distribution.

Notes: the build machine needs PyInstaller (`python -m pip install pyinstaller`). The Electron runtime zip is downloaded from the npmmirror mirror and cached under `%LOCALAPPDATA%\electron\Cache` to avoid depending on GitHub connectivity.

## Tests

```powershell
npm.cmd run test:python
npm.cmd test
npm.cmd run build
npm.cmd audit --audit-level=high
```

## In-app configuration

After starting the desktop app, open "Connections & keys" in the top right. Everything is configured there — no environment variables or extra terminals:

- Coordinator model: OpenAI-compatible Base URL, model name and API key.
- Tushare: token used by the company & industry agent for security identity, industry, company info, valuation, financials and performance forecasts.
- Tavily: API key used by the company & industry agent for recent industry background.
- Every entry can be "Save & test"; changes apply without restarting the harness.

Once configured:

- The coordinator picks the experts it needs from the capability set the current report allows.
- Model output must pass structured validation; on failure it automatically falls back to local capability rules.
- Quant facts are still computed by deterministic code; the model cannot override parsing results.
- Tushare and Tavily are exposed to the company & industry agent as read-only tools; the global-markets agent reads delayed public index quotes through an isolated data adapter. Failed or denied queries are marked unknown item by item, never fabricated.

## Local vector knowledge base

The vector store can live entirely on this machine, and ingestion does not require manual per-document embedding. The code currently ships an automatic ingestion demo, intended mainly for the company & industry material library:

```text
live retrieval results → content-hash dedup → auto chunking → local vectorization → SQLite storage → hybrid retrieval on later runs
```

The current demo uses the zero-dependency `local-hashing-v1` feature vectors to validate the ingestion, dedup, indexing, recall and audit architecture first; it is not a production-grade semantic model. The same interface can later be swapped for a local Ollama embedding or SentenceTransformers, and the vector store moved to Qdrant/pgvector as the data grows. Users only choose allowed sources, refresh frequency and the model in-app; the recommended human-maintained inputs are a trusted-source allowlist and a small evaluation set, not hand-built vectors.

## Security boundaries

- v1 only reads reports the user pastes; no connection to legacy servers, email or trading interfaces.
- The renderer has no Node access; it only reaches the whitelisted IPC through a context-isolated preload.
- The renderer runs sandboxed with CSP; IPC validates senders, permission requests default to deny, and external links only open credential-free HTTPS.
- The Python harness exposes no shell, file write, arbitrary Python or order-placement tools to agents.
- Keys are encrypted with Windows DPAPI at current-user scope and are never echoed into the UI, chat records or run events.
- Hidden chains of thought are not persisted; only conclusions, structured details, tasks, status and errors are stored.
- Output is research interpretation and risk flags only — not buy/sell or position advice.

## Repository layout

```text
apps/desktop/                 Electron main process, preload, React UI
services/harness/src/         Python parser, harness, model adapters, persistence
services/harness/tests/       Python unit and dynamic orchestration tests
packages/protocol/            cross-process protocol notes and future generated types
docs/ARCHITECTURE.md          detailed architecture and evolution boundaries
docs/ENTERPRISE_ROADMAP.md    enterprise gaps, production roadmap and resume framing
```

## Product documentation

The PM-side artifacts of this project (in Chinese) live under `docs/` and in the repo root:

- `docs/PRD.md` — product requirements document v0.2, revised after the first user interview (4 hypotheses tested, 3 overturned), adding interview-driven requirements: decision cards, watchlist, batch processing, effectiveness reporting.
- `docs/COMPETITIVE_ANALYSIS.md` — six competitor categories (Eastmoney, 同花顺问财, Wind/iFinD, 慧博, general LLMs, the PTrade ecosystem) and the revised differentiation: report-level end-to-end automation + deterministic rule computation + data-source neutrality + human-in-the-loop boundaries.
- `docs/LAUNCH_PLAN.md` — seed-user plan, phase-2 interview plan, 4–8 week validation cadence and Go/No-Go criteria.
- `docs/INTERVIEW_GUIDE.md` — the two-phase interview guide.
- `量化研究员深度访谈记录.md` — full transcript of the 28-minute first interview (anonymized).

## Next milestones

1. LangGraph/PostgreSQL checkpoints, idempotent tool calls, and crash recovery.
2. OpenTelemetry trace/metric/log export with quality and cost alerts.
3. Offline evaluation set, prompt/model version comparison, and CI quality gates.
4. Organization-level SSO/RBAC, approval flows, Vault/KMS and multi-project isolation.
5. Signed Windows installers, auto-update, SBOM and supply-chain security scanning.
