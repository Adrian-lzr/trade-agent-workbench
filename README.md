
<div align="left">

```diff
+  ╔╦╗╔═╗╔╦╗╔═╗  ╔═╗╔═╗╔═╗╔╗╔╔╦╗
+   ║║╠═╣ ║ ╠═╣  ╠═╣║ ╦║╣ ║║║ ║
+  ═╩╝╩ ╩ ╩ ╩ ╩  ╩ ╩╚═╝╚═╝╝╚╝ ╩

[ Natural Language → SQL Query Agent ]
```

</div>

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)



---

A natural language to SQL (NL2SQL) platform built on LangGraph and Azure OpenAI. This multi-agent system automatically routes user questions to the appropriate database backend and generates optimized SQL queries and results.

Built on top of LangChain's [`SQLDatabase`](https://docs.langchain.com/oss/python/langchain/sql-agent) with extended support for Azure AD authentication, Cosmos DB, and built-in dialect validation.

## Trade Agent Development

The `trade_agent` package is implemented alongside the upstream NL2SQL project. M1 provides immutable, provenance-aware RFQ and quote contracts, deterministic catalog matching, Decimal pricing, quote-draft quality gates, append-only revision persistence, and synthetic catalog data. M2-01 through M2-06 add bounded LangGraph state, a fixture-driven Fake gateway, evidence-backed clarification and product decisions, PostgreSQL checkpoint recovery, durable Run/Event lifecycle with fenced worker leases, idempotent recovery, and reply fact consistency. M3 adds the FastAPI Run/Event contract, Vue review workbench, immutable hash-bound approvals, fixed-template private PDF artifacts, read-only analytics views, a DB-backed worker, and a Compose app profile. M4 adds the synthetic evaluation set, deterministic evaluator, integration evidence, and demo documentation.

### Offline quote-draft demonstration

This path does not require Docker, PostgreSQL, or an LLM. It uses synthetic RFQ, product, and price data only. The selected SKU is the salesperson's explicit confirmation; a specification conflict, unknown required specification, missing quantity/unit/price, or invalid quote validity prevents quote creation.

```powershell
uv sync --all-extras
uv run python scripts/demo_quote_draft.py --sku BOLT-M8-30-A2 --quantity 5000
```

The example produces a synthetic draft line for `400.00 USD` with catalog and price-list provenance. This amount is an algorithm fixture, not a commercial quotation.

### Local PostgreSQL

Use Docker Desktop with the WSL 2 engine running. The Compose file binds PostgreSQL to localhost and uses a local-only development password.

In PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
uv sync --all-extras
uv run alembic upgrade head
uv run python scripts/seed_synthetic_trade_data.py
```

The seed contains synthetic demonstration data only. Do not use it for customer quotations. The model gateway defaults to `TRADE_MODEL_MODE=fake`; fake responses must be supplied as explicit fixtures, so arbitrary inquiries are never answered with canned data.

LangGraph checkpoints use a restricted MessagePack type policy. Keep `LANGGRAPH_STRICT_MSGPACK=true` in the environment when running a workflow process.

### Model gateway configuration

The default Fake gateway makes no network calls and needs no credentials. Tests inject scripted extraction and draft responses. For a real provider, set `TRADE_MODEL_MODE=openai` or `TRADE_MODEL_MODE=azure_openai`, then configure:

| Variable | OpenAI | Azure OpenAI |
| --- | --- | --- |
| `TRADE_MODEL_API_KEY` | Required | Required |
| `TRADE_MODEL_MODEL_NAME` | Required | Not used |
| `TRADE_MODEL_BASE_URL` | Optional | Not used |
| `TRADE_MODEL_AZURE_ENDPOINT` | Not used | Required |
| `TRADE_MODEL_AZURE_DEPLOYMENT` | Not used | Required |
| `TRADE_MODEL_AZURE_API_VERSION` | Not used | Optional; defaults to `2024-12-01-preview` |
| `TRADE_MODEL_MAX_REQUESTS_PER_INSTANCE` | Required | Required |
| `TRADE_MODEL_MAX_OUTPUT_TOKENS_PER_REQUEST` | Required | Required |

The request cap is held in memory per gateway instance and the output cap is sent to the provider for each call. They bound this local adapter but do not set an account-wide monetary budget; configure provider-side spending limits separately. Provider retries are disabled, and malformed structured extraction can be retried at most once. No real model is called by the test suite.

The integration suite uses a separate `trade_agent_test` database on localhost port 5433. Its fixture resets the `trade_agent` schema and Alembic version table only inside that database. The current development migration head is `0009_inquiry_fanout`; the integration fixture applies the same head to its disposable database for each run. Start the isolated service and run the tests with:

```powershell
docker compose --profile integration up -d postgres-test
uv run --env-file .env pytest tests/trade_agent/integration -o addopts='' -q
```

Do not point `TRADE_TEST_DATABASE_URL` at the development database.

### Full local app profile

After applying migrations, provision the dedicated analytics login as the database owner. Replace the two `replace-me` literals in `scripts/provision_analytics_role.sql` with a local secret; the checked-in `.env.example` uses `local-dev-only` only for disposable development containers. The role receives `SELECT` on the three reporting views and no access to source tables.

```powershell
docker compose --env-file .env --profile app up -d
docker compose --env-file .env --profile app ps
```

The API is at `http://127.0.0.1:8000`, the workbench is at
`http://127.0.0.1:5173`, and the OpenAPI document is at
`http://127.0.0.1:8000/openapi.json`. The frontend build defaults to demo mode
and uses its same-origin `/api` nginx proxy when API mode is enabled. Configure
`VITE_API_MODE=api` at image build time to enable API requests. The trusted
identity headers must come from an authenticated upstream; do not pass
`VITE_TRUSTED_*` values to the frontend image because Vite embeds them in
public browser assets. Set `TRADE_ANALYTICS_MODE=real` only after the dedicated
login has been provisioned. The default `fake` analytics mode labels every
result as synthetic.

### Run/Event API slice

The current API exposes the durable Run/Event boundary. Run it with the Fake gateway and local database:

```powershell
uv run uvicorn trade_agent.api.app:app --host 127.0.0.1 --port 8000
```

Each request must carry trusted upstream assertions `X-Org-Id`, `X-Actor-Id`, and `X-Role` (`sales`, `reviewer`, or `admin`). `POST /api/v1/runs` is restricted to `sales`; reads and resume are restricted to `sales` or `reviewer`. Resume requires an `Idempotency-Key` header or a body `event_key`. FastAPI publishes the typed OpenAPI contract at `/docs` and `/openapi.json`. This boundary does not send email or invoke a worker by itself.

### Docker lifecycle rule

Exit Docker Desktop normally every time: use the tray **Quit** action or `docker desktop stop`, then wait for it to finish stopping before shutting down or restarting Windows. Never use Task Manager **End task**, `taskkill /F`, `Stop-Process -Force`, or **Reset to factory defaults**. Do not delete Docker data, unregister WSL distributions, or run scripts that recursively clean Docker runtime paths.

If the Secrets Engine `engine.sock` startup error returns, stop Docker normally first when possible, then rename the containing `docker-secrets-engine` directory as a whole to a unique sibling backup and keep that backup. Do not open, delete, rename, or remove reparse points from the socket entry itself; do not use `robocopy /MIR`. Let Docker recreate its runtime directory on the next normal start. The root-level experimental Docker repair scripts are not part of this procedure and must not be run.

## Features

- **Multi-Database Support**: PostgreSQL, Azure SQL, Azure Synapse, Azure Cosmos DB, Databricks SQL, and Google BigQuery
- **Intent Detection**: Automatically routes queries to the correct data agent based on question context
- **Multi-Turn Conversations**: Follow-up questions with context awareness (e.g., "What's the average?" after a query)
- **SQL Validation**: Safe query execution with sqlglot-based validation across all dialects
- **Data Visualization**: Generate charts and graphs from query results using natural language (e.g., "show me a bar chart")
- **Configurable Agents**: YAML-based configuration for adding new data sources
- **A2A Protocol**: Agent-to-Agent interoperability for integration with other A2A-compliant systems

## Architecture

### Intent Detection Flow

Routes user questions to the appropriate data agent based on context.

![Intent Detection Flow](docs/intent_detection_graph.png)

### Data Agent Flow

Generates, validates, and executes SQL queries with retry logic.

![Data Agent Flow](docs/data_agent_graph.png)

## Documentation

- [Database Setup](docs/DATABASE_SETUP.md)
- [Configuration](docs/CONFIGURATION.md)
- [Data Visualization](docs/VISUALIZATION.md)
- [A2A Protocol](docs/A2A.md)

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Azure OpenAI deployment

### Installation

```bash
git clone https://github.com/eosho/langchain_data_agent
cd langchain_data_agent
uv sync --all-extras
cp .env.example .env
# Edit .env with your values
```

### CLI Usage

The CLI provides commands for querying data agents through natural language.

```bash
# Show available commands
data-agent --help
```

**Commands:**

| Command | Description |
|---------|-------------|
| `query` | Run a single query and exit |
| `chat` | Start interactive chat mode |
| `configs` | List available configurations |
| `validate` | Validate configuration files |

**Options:**

| Option | Description |
|--------|-------------|
| `-c, --config` | Configuration to use (default: loads all configs) |
| `-v/-q, --verbose/--quiet` | Show/hide query state (agent, SQL, message history) |
| `-l, --log-level` | Logging level (debug, info, warning, error) |

Each config file contains multiple specialized data agents. The system automatically routes your question to the appropriate agent.

| Config | Agents | Domain |
|--------|--------|--------|
| `contoso` | `contoso_sales`, `contoso_products`, `contoso_inventory` | Retail data (Databricks, Cosmos DB, PostgreSQL) |
| `adventure_works` | `contoso_hr`, `hotel_analytics` | HR & hotel data (Azure SQL, Synapse) |
| `amex` | `financial_transactions` | Financial data (BigQuery) |

**Example Questions by Config:**

<details>
<summary><b>contoso</b> (default)</summary>

**contoso_sales** (Databricks)
1. How many orders were placed last month?
2. What is the total revenue by region?

**contoso_products** (Cosmos DB)
1. How many active products are there?
2. Find all products with low inventory
3. What is the most expensive product?

**contoso_inventory** (PostgreSQL)
1. Which products need to be reordered?
2. What is the total inventory by warehouse?
3. How many shipments are in transit?

```bash
# Single query (loads all configs by default)
data-agent query "How many shipments are in transit?"

# Interactive chat
data-agent chat

# Use specific config
data-agent query "How many shipments are in transit?" -c contoso
data-agent chat -c contoso
```

</details>

<details>
<summary><b>adventure_works</b></summary>

**contoso_hr** (Azure SQL)
1. How many employees are in each department?
2. What is the average salary by department?
3. Who are the top performers this year?

**hotel_analytics** (Synapse)
1. What is the total revenue by hotel?
2. What is the revenue breakdown by booking channel?
3. What is the average daily rate by room type?

```bash
data-agent query "What is the total revenue by hotel?" -c adventure_works
data-agent chat -c adventure_works
```

</details>

<details>
<summary><b>amex</b></summary>

**financial_transactions** (BigQuery)
1. What are the total deposits by customer segment?
2. Show me all high-severity fraud alerts from the past week
3. Who are the top 5 customers by transaction volume?
4. Show me a bar chart of transactions by type

```bash
data-agent query "What are the total deposits by customer segment?" -c amex
data-agent chat -c amex
```

</details>

```bash
# List available configs
data-agent configs

# Validate configuration files
data-agent validate           # Validate all configs
data-agent validate contoso   # Validate specific config
```

### Chainlit Web UI

The platform includes a Chainlit-based web interface for interactive data exploration.

```bash
# Start the Chainlit UI
chainlit run src/data_agent/ui/app.py
```

**Available Profiles:**

| Profile | Description |
|---------|-------------|
| Contoso | Retail sales database with products, customers, and orders |
| Amex | Financial transactions and merchant data |
| Adventure Works | Sample database with sales and product information |

**Environment Variables:**

Ensure these are set in your `.env` file:

```bash
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

### Programmatic Usage

```python
import asyncio
from data_agent import DataAgentFlow

async def main():
    async with DataAgentFlow(
        config_path="data_agent/config/contoso.yaml",
        azure_endpoint="https://your-resource.openai.azure.com/",
        api_key="your-api-key",
        deployment_name="gpt-4o",
    ) as flow:
        result = await flow.query("Show me all warehouses")
        print(result["final_response"])

asyncio.run(main())
```

## Supported Databases

The platform includes built-in configuration for these databases:

| Database | Datasource Type | SQL Dialect |
|----------|-----------------|-------------|
| PostgreSQL | `postgres` | postgres |
| Azure SQL | `azure_sql` | tsql |
| Azure Synapse | `synapse` | tsql |
| Azure Cosmos DB | `cosmos` | cosmosdb |
| Databricks SQL | `databricks` | databricks |
| Google BigQuery | `bigquery` | bigquery |
| MySQL | `mysql` | mysql |
| SQLite | `sqlite` | sqlite |

> **Note:** Any SQLAlchemy-compatible database can be used via `shared_db` parameter or `connection_string` in config. The built-in types provide convenience configuration and AAD authentication support.

## Development

```bash
# Format and lint
uv run pre-commit run --all-files

# Run tests
uv run pytest
```

## License

MIT License - see LICENSE file for details.
