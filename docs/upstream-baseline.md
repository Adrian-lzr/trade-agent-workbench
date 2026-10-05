# Upstream Baseline

This document records the checked-out `eosho/langchain_data_agent` baseline for the trade-sales-agent implementation plan. The plan is a target design, not evidence that its features already exist.

## Revision and environment

| Item | Observed baseline |
| --- | --- |
| Upstream commit | `9081abf1f118a18b9152b5966ce1c65c7a704baf` |
| Python used for dependency sync | CPython 3.12.10 |
| Dependency sync | `uv sync --all-extras` succeeded; it reconciled `uv.lock` with the declared project version (`pyproject.toml` 0.3.1 vs lock 0.3.0 at the checked-out commit) |
| Import smoke check | `uv run python -c "import data_agent"` succeeded |
| Pytest | `uv run pytest -q` collected no tests and exited nonzero because configured coverage was 0%, below the 50% minimum |
| Ruff | `uv run ruff check --no-fix --statistics src tests` reported 226 violations; this was a read-only check |
| Docker | Docker Desktop 4.93.0, CLI 29.8.1, and Compose are installed. WSL and Virtual Machine Platform are enabled; after restarting Windows, `HypervisorPresent=True`, `docker desktop status` reports `running`, and `docker version` returns Engine 29.8.1. PostgreSQL 17.11 development and integration containers are healthy on localhost ports 5432 and 5433. |
| Remote fork | Not created: GitHub CLI credentials were invalid at the time of the check; a later refresh attempt was reset by the remote during device-code exchange. |

The source checkout is at the commit above. These results describe this environment and do not imply database, model-provider, Azure session, or UI end-to-end connectivity. The baseline test command generated `.coverage`, which was cleared with `uv run coverage erase`; dependency synchronization updated `uv.lock` as noted above.

## Upstream capabilities and boundaries

| Area | Source observation | Implication for the trade workflow |
| --- | --- | --- |
| Entry point and routing | `src/data_agent/agent.py` defines `DataAgentFlow`. It initializes configured datasources and data-agent graphs, then routes natural-language questions through intent detection, query rewrite, and a selected agent. `src/data_agent/graph.py` exposes `create_data_agent(...)` for one datasource/configuration. | Reuse as a separate, later read-only analytics path. It is not an inquiry-to-quote workflow and should not own quote facts or approvals. |
| Graph and state | `DataAgentGraph` in `graph.py` builds `generate_sql -> validate_sql -> execute_query`, with retry and optional visualization/response nodes. `models/state.py` defines NL2SQL `InputState`, `AgentState`, and `OutputState` fields such as question, datasource, SQL, query result, and response. Both the graph factory and `DataAgentFlow` default to `InMemorySaver`. | The trade workflow needs its own domain state and graph. The current in-memory checkpointer is process-local and is not durable restart recovery. |
| Model provider | `llm/provider.py` implements `AzureOpenAIProvider`; `llm/base.py` contains the provider factory. The Chainlit UI reads Azure OpenAI endpoint, key, and deployment environment variables. | The checked code does not provide the plan's provider-neutral `ModelGateway` or a Fake provider. Do not imply those are present. |
| SQL validation | `validators/sql_validator.py` parses supported dialects using SQLGlot, allows read-only SELECT forms, checks blocked functions, and enforces a row limit. Dialects listed for basic validation use a simpler SELECT-prefix/write-keyword check. | This validator guards generated analytical queries; it is not business authorization or a substitute for database permissions. A future analytics connection should still be restricted to read-only views/user privileges. |
| Executor | `executors/local.py` runs generated Python via `exec()` on the host, and its `timeout` is not enforced. `executors/__init__.py` selects it unless Azure sessions are configured. `azure_sessions.py` calls Azure Container Apps Dynamic Sessions and requires an endpoint and usable Azure authentication. | Treat visualization code execution as a distinct capability. Keep it unavailable to the trade graph/UI unless explicitly reviewed and isolated; local mode is not sandboxed. Azure isolation was not integration-tested here. |
| UI | `src/data_agent/ui/app.py` is a Chainlit chat UI. It loads existing data-agent configurations, requires Azure OpenAI credentials, creates `DataAgentFlow`, and can show SQL/results/generated visualization code and images. | This is an existing NL2SQL chat surface, not the plan's quote-review workbench. No Vue/React review UI or approval workflow is present. |
| Packaging | `pyproject.toml` requires Python `>=3.12`, uses setuptools, discovers `data_agent*` under `src`, and defines `data-agent`, `data-agent-ui`, and `data-agent-a2a` scripts. Runtime dependencies include LangGraph, Chainlit, and multiple datasource/provider integrations. | New packages outside the `data_agent*` pattern will not be included automatically. Any package-layout change must be verified in a built wheel. |
| License | `LICENSE` contains the MIT License and copyright notice for Ernest Oshokoya (2025). | Preserve the upstream license and copyright notice in distributions and derivative copies; review attribution requirements when adding files or redistributing. |

## Plan-to-baseline gap summary

The baseline supplies a configurable NL2SQL agent with multi-datasource adapters, an in-memory LangGraph checkpointer, SQL validation, optional visualization, a Chainlit UI, and setuptools packaging. It does not supply the plan's trade domain entities, deterministic `Decimal` pricing, quote revisions, audit trail, reviewer authorization, durable trade-graph recovery, Fake model path, quote export, or review UI.

The plan's architecture remains compatible with keeping NL2SQL as a later, isolated analytics capability. Implement a separate trade graph and persistence/API/UI path; expose only authorized read-only analytical data to the upstream flow. Do not use model-generated SQL or visualization Python execution as a trade business write path.

## M0 completion notes

- **M0-01:** The local branch `codex/trade-agent` is based on the pinned commit and has an `upstream` remote. The GitHub fork was not created because the stored GitHub CLI token is invalid. Earlier `gh auth refresh` device-code exchanges were reset; a fresh attempt on 2026-09-29 was refused by the device-code endpoint even though a separate TCP probe to `github.com:443` succeeded. Retry when the authorization endpoint accepts connections and account reauthentication succeeds.
- **M0-02:** Commit, MIT license, dependency setup, import, pytest, and Ruff results are recorded above.
- **M0-03:** Component boundaries and intended reuse are recorded above and in [ADR 0001](adr/0001-upstream-boundaries.md).
- **M0-04:** Added `docker-compose.yml` with digest-pinned PostgreSQL 17.11 Alpine development and isolated integration-test services, both bound to localhost. `.env.example` includes separate development and `trade_agent_test` connection settings. `docker compose --env-file .env.example config --services` passed for both the default and `integration` profile; the profile adds only the dedicated `postgres-test` service. Docker Desktop 4.93.0 and CLI 29.8.1 are installed. WSL and Virtual Machine Platform are enabled; after restarting Windows, `HypervisorPresent=True`, Desktop reports `running`, and `docker version` returns Engine 29.8.1. Both PostgreSQL containers are healthy on localhost ports 5432 and 5433. The development database is migrated through `0002_rfq_quote_revisions` and seeded with 60 synthetic products and 120 synthetic price tiers. `MODEL_MODE=fake` is reserved in the example configuration; the Fake gateway is a later M2 feature and is not implemented in this slice.
- **M0-05:** The trade workbench will use Vue 3 + TypeScript + Vite, with shadcn-vue components and Tailwind tokens; the customer quote artifact will be a fixed-template PDF generated from the approved snapshot with ReportLab. On 2026-09-29 the npm registry reported Vue 3.5.43, TypeScript 7.0.2, Vite 8.3.1, `@vitejs/plugin-vue` 6.0.9 (supports Vite 5-8 and Node `^20.19.0 || >=22.12.0`), `vue-tsc` 3.3.11 (TypeScript `>=5`), shadcn-vue 2.8.2, and Tailwind 4.3.3. PyPI reported ReportLab 5.0.1 with Python `>=3.9`; the checked runtime is Python 3.12.10. These registry versions were checked, not installed or tested in a frontend/PDF build; lock compatible versions when those features are implemented.

Accordingly, source and baseline inspection is documented, but the full M0 exit condition is not yet met: the GitHub fork is pending authentication, and the Fake gateway is scheduled for M2. Docker engine startup, PostgreSQL migrations, and synthetic data seeding are verified. Current implementation and verification progress is recorded in [implementation-progress.md](implementation-progress.md).

## Source references

- `src/data_agent/agent.py`
- `src/data_agent/graph.py`
- `src/data_agent/models/state.py`
- `src/data_agent/llm/base.py` and `src/data_agent/llm/provider.py`
- `src/data_agent/validators/sql_validator.py`
- `src/data_agent/executors/`
- `src/data_agent/nodes/visualization.py`
- `src/data_agent/ui/app.py`
- `pyproject.toml`
- `LICENSE`
