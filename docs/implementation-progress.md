# Implementation Progress

Updated: 2026-10-01

The master plan is the design and acceptance contract. This file records observed implementation and verification state; a skipped integration test is not a pass.

## M0: Environment and Baseline

| Work | State | Evidence or remaining work |
| --- | --- | --- |
| M0-01 Fork and pinned upstream baseline | Partial | Local branch is based on `9081abf1f118a18b9152b5966ce1c65c7a704baf`; GitHub fork is pending account reauthentication. `gh auth status` confirms the stored token is invalid. A fresh `gh auth refresh --hostname github.com --scopes repo` attempt was refused by the device-code endpoint on 2026-09-29, so no fork was created. |
| M0-02 Dependency and upstream baseline checks | Done | `uv sync --all-extras` and import smoke check passed; the baseline pytest invocation failed its configured coverage threshold because no tests were collected. Results are in [upstream-baseline.md](upstream-baseline.md). |
| M0-03 Upstream boundary review | Done | Recorded in `docs/adr/0001-upstream-boundaries.md`. |
| M0-04 Local database and synthetic data setup | Partial | Compose, environment example, migrations, a dedicated `trade_agent_test` database service, and a 60-SKU/120-tier synthetic seed are present. Docker Desktop 4.93.0 and CLI 29.8.1 are installed. The one-time Windows restart required to load the hypervisor has already occurred; current policy is to stop Docker normally before any later shutdown or restart. Engine 29.8.1 responds, and development/test PostgreSQL containers are healthy on localhost ports 5432/5433. The development and test schemas migrate through `0013_shipment_handoff`; the development database was upgraded and the synthetic seed was re-run successfully. |
| M0-05 UI and export choices | Decision recorded | Vue 3 + TypeScript + Vite with shadcn-vue/Tailwind, and fixed-template PDF with ReportLab are recorded in `docs/upstream-baseline.md`; they are not implemented in M0. |

M0 exit criteria remain pending GitHub fork authentication. The Docker/PostgreSQL environment is verified; M2-02 adds the gateway boundary without making external model calls.

Docker lifecycle guard: use tray Quit or `docker desktop stop`; never force-kill Docker, shut down/restart Windows while it is running, or use factory reset. For a recurrence of the Secrets Engine socket startup failure, preserve the runtime directory by renaming its parent directory as a whole; do not manipulate the socket entry, recursively delete runtime paths, or mirror-delete them. No Docker repair scheduled task is currently registered. Legacy root-level repair scripts were inspected but not run because some contain forced process termination, WSL shutdown, or recursive deletion.

## M1: Deterministic Business Core

M1-01 through M1-06 have code and focused tests. The deterministic path now accepts an RFQ revision plus explicit salesperson SKU selections, blocks unresolved facts/specifications and missing prices, and creates a sourced `QuoteRevision` snapshot. `uv run python scripts/demo_quote_draft.py --sku BOLT-M8-30-A2 --quantity 5000` demonstrates the synthetic 400.00 USD line without Docker, PostgreSQL, or an LLM; selecting the A4-80 SKU instead returns `SPECIFICATION_CONFLICT` and no quote. An offline regression also verifies that `5 boxes` without a configured box-to-piece conversion retains its original quantity/unit and cannot create a quote. M2-03 now persists human clarifications and routes audited product choices into the M1 quote path. The PostgreSQL write path appends revisions, checks expected current versions, records idempotency and audit data, and uses database triggers to reject revision/audit updates and deletes.

| Verification | Result |
| --- | --- |
| `uv run pytest tests/trade_agent/unit -o addopts='' -q` | 49 passed |
| `uv run --env-file .env.example pytest tests/trade_agent/integration -o addopts='' -q` | 21 passed against the isolated PostgreSQL 17.11 service on localhost:5433 |
| `docker compose --env-file .env.example config --services` (default and `integration` profile) | Passed; the profile adds isolated `postgres-test` on localhost port 5433 |
| `uv run --env-file .env.example` configuration smoke check | Passed; the dedicated test URL loads as `trade_agent_test` on port 5433 |
| `uv run ruff check --no-fix src/trade_agent tests/trade_agent migrations` | Passed |
| `uv run ruff format --check src/trade_agent tests/trade_agent migrations` | Passed |
| `uv lock --check` | Passed |
| `uv run alembic upgrade head --sql` | Passed; offline PostgreSQL DDL generated through `0009_inquiry_fanout` |
| `uv run --env-file .env.example alembic upgrade head` | Passed against the separate development database; schema is at `0009_inquiry_fanout` |
| `uv run --env-file .env.example python scripts/seed_synthetic_trade_data.py` | Passed; 60 synthetic products and 120 synthetic price tiers loaded |
| Docker Desktop engine | Passed after Windows restart; Desktop 4.93.0 reports running and Docker Engine 29.8.1 responds |
| Wheel build/import smoke check | Passed; built wheel contains and imports `trade_agent.modeling` and `trade_agent.workflow` alongside the existing packages |
| Offline M1 demo | Passed for the A2-70 400.00 USD draft and the A4-80 hard-conflict blocker |
| `uv run ruff check --no-fix --no-force-exclude scripts/demo_quote_draft.py` | Passed |

M1 implementation and its PostgreSQL exit checks are complete: the deterministic quote path has offline coverage, its original 9 integration cases pass as part of the current 21-case integration suite, and the development database is migrated and seeded. The integration fixtures reset `trade_agent` and `alembic_version` only inside the dedicated `trade_agent_test` database. M0-01 fork authentication remains a separate external blocker.

## M2: Inquiry Workflow and Human Review

| Work | State | Evidence or remaining work |
| --- | --- | --- |
| M2-01 Graph state, nodes, and explicit routes | Done | `src/trade_agent/workflow/` has bounded state, callback validation, explicit error routes, and distinct clarification, product-selection, and quote-review waits. In-memory persistence is used in unit tests; M2-04 supplies PostgreSQL persistence for integration/runtime use. |
| M2-02 Fake/real `ModelGateway`, prompt versions, structured extraction | Done | `src/trade_agent/modeling.py` defines the narrow interface, scripted offline Fake, LangChain OpenAI/Azure OpenAI adapters, versioned prompts, usage/latency/error metadata, schema parsing with one bounded parse retry, caller-owned IDs, and source-quote checks. Real providers require explicit credentials plus per-instance request and per-request output-token caps. Fake is the default and fails closed unless a test supplies a fixture. |
| M2-03 Source evidence and human clarification/product decisions | Done | Evidence quotes must occur in their cited body line/range; field names and source document IDs are server-checked. `db/clarifications.py` creates append-only child revisions with authenticated actor attribution, current-version checks, audit records, and command-level idempotency. Product decisions are idempotent/audited, reject incomplete or conflicting specs, and `workflow/quote.py` sends saved decisions through the M1 price/quality path. T02/T03/T11 regressions pass; clarification keeps unresolved units and original item/source evidence. |
| M2-04 PostgreSQL checkpoints and resumable threads | Done | `workflow/checkpointing.py` opens the official PostgreSQL saver with an explicit restricted MessagePack allowlist; stable UUID `run_id` maps to `thread_id`. A two-process integration test pauses at clarification, exits, reopens the saver, and resumes with `Command(resume=...)` on the same thread. Checkpoint rows are isolated in the dedicated `trade_agent_test` DB. |
| M2-05 Run/Event lifecycle, single executor, idempotent recovery | Done at persistence/integration-test level | `db/runs.py` and migrations `0004_run_lifecycle`/`0005_run_fencing_quote_actions` add queued/running/waiting_input/succeeded/failed/cancelled states, worker leases/heartbeats, fenced lease generations, row-locked claims, append-only `(run_id,event_seq)` events, stable event-key replay, expired-lease requeue, and the `run_id + action_type + business_version` quote-action key. `test_run_lifecycle.py` and `test_revisions.py` cover stale-worker rejection, single-worker exclusion, repeated resume, lease recovery, contiguous event history, immutable events, and quote-action replay. |
| M2-06 Reply-draft fact consistency | Done at gateway/unit-test level | `ModelGateway.draft_reply` now validates dates, amounts, quantities, currencies, trade terms, delivery/availability claims, and certification claims against the customer-visible `QuoteRevision` snapshot. Unsupported facts fail closed with `model.reply_fact_mismatch`; T14 regression covers `400.00` becoming `40.00`, plus unsupported delivery/certification claims. |

Design reference reviewed 2026-09-29: the official [LangGraph PostgreSQL checkpoint implementation](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres) documents saver setup, persistent thread IDs, and strict MessagePack deserialization. The project adopts those boundaries and tests process restart; core dependencies were raised to versions that support the strict serializer policy. The [Agentailor LangGraph.js template](https://github.com/agentailor/fullstack-langgraph-nextjs-agent) was also reviewed for persistent threads and approval pauses; its JavaScript UI/auth design is outside the current Python workflow slice and was not copied.

| Verification | Result |
| --- | --- |
| `uv run --env-file .env.example pytest tests/trade_agent -o addopts='' -q` | 159 passed against the disposable PostgreSQL service, including approval, PDF artifact, analytics, worker, checkpoint, SLA, qualification, document consistency, and shipment handoff coverage |
| Ruff check and format check for the changed `trade_agent`/migration paths | Passed; the unchanged upstream `data_agent` tree still has its pre-existing strict-lint findings |
| `uv lock --check` and `git diff --check` | Passed after the LangGraph/checkpointer compatibility update |
| `uv run --env-file .env.example` gateway configuration smoke check | Passed; default mode is `fake` and constructs `FakeModelGateway` without credentials |
| Wheel build/import smoke check | Passed; the wheel contains and imports `trade_agent.db.clarifications`, `trade_agent.db.decisions`, `trade_agent.workflow.checkpointing`, and `trade_agent.workflow.quote` |
| Real provider call | Not run; tests use deterministic fakes and no API key was used |

M2-01 through M2-06 are complete at the library/integration-test level. M3-01 exposes the Run/Event contract through FastAPI with trusted upstream identity headers, organization/role checks, uniform error bodies, paged event reads, and idempotent create/resume requests. The graph is now paired with a DB-backed worker process; M3-02 through M3-06 add the review workbench, approval/export APIs, reporting views, and Compose app profile. M0-01 fork authentication remains a separate external blocker. No customer data has been added and no email-sending path exists.

## M3: Formal Delivery and Upstream Integration

| Work | State | Evidence or remaining work |
| --- | --- | --- |
| M3-01 FastAPI identity boundary, pagination, errors, and request idempotency | Done for the Run/Event slice | `src/trade_agent/api/app.py` provides `POST /api/v1/runs`, `GET /api/v1/runs/{run_id}`, `GET /api/v1/runs/{run_id}/events`, and `POST /api/v1/runs/{run_id}/resume`. Tests cover missing/invalid identity, role and organization boundaries, stable request IDs, paged events without payload/actor leakage, idempotent create/resume, and conflicts. OpenAPI is generated by FastAPI from the typed route models. |
| M3-02 Review workbench | Done with service boundary caveat | Vue 3 + TypeScript workbench has inquiry, review, approval, export, and analytics views, responsive desktop/mobile layout, explicit demo/API status, and trusted identity configuration. Browser build and Playwright desktop/mobile checks passed. |
| M3-03 Approval transactions | Done | `quote_approvals` binds one immutable revision and content hash; reviewer/admin scope, stale revision/hash conflicts, immutable decisions, idempotency, and concurrent single-winner behavior are covered in PostgreSQL tests. |
| M3-04 Fixed-template export | Done | ReportLab `proforma-v1` PDF is rendered only from the approved snapshot, stores Decimal total and SHA-256, supports ready replay and failed retry, rejects unapproved/pending downloads, and is private by organization. |
| M3-05 Read-only analytics | Done with deployment step | Three allow-listed metrics run over security-barrier views with bound filters and explicit Fake/real labels. The provisioned analytics login can read views but cannot read source tables or insert into them; production credentials must be supplied outside the repository. |
| M3-06 Compose delivery | Done with image-build network caveat | Root/API/worker and frontend Dockerfiles, health checks, pinned PostgreSQL image, and `app`/`integration` profiles are present. `docker compose --profile app config` passes; image build was attempted but Docker Hub token retrieval timed out in this environment. |

## Evidence-backed expansion backlog

The external research recorded in [GitHub trade workflow research](research/2026-10-01-github-trade-workflows.md) and [export workflow evidence](research/2026-10-01-export-workflow-evidence.md) changes the next product priorities without silently expanding the MVP:

| Priority | Proposed capability | Evidence and acceptance boundary |
| --- | --- | --- |
| P0 after M3 | Customer qualification and screening evidence | ITA says buyer vetting and the Consolidated Screening List reduce legal, financial, and reputation risk. Store source, check time, result, reviewer, and attachment; an unresolved hit blocks quote-to-shipment progression. |
| P0 after M3 | Pro forma invoice, commercial invoice, and packing-list consistency | ITA describes the documents and the fields customs compares. Generate each from an approved immutable snapshot and block mismatched quantities, descriptions, weights, or invoice references. |
| P0 after M3 | Inquiry response SLA and translation review | ITA describes systematic inquiry handling, customer-role research, timely complete replies, and human review of translated commercial text. Add received timezone, response due time, customer role, original language, translation-review state, versioned reply, attachments, and a nurture/overdue queue. |
| P0 after M3 | Shipment handoff and freight evidence | ITA shipping guidance points to Incoterm responsibility, freight-forwarder offers, insurance, packaging/labels, booking, ETA, and export/import document checks. Add a shipment gate and keep unresolved document/insurance checks visible. |
| P1 | Payment-risk gates and due-date tasks | ITA documents open-account, collection, letter-of-credit, and other payment risks. Add payment method, terms, deposit/balance conditions, insurance/guarantee evidence, and audited overdue tasks. |
| P1 | Follow-up activities with stop conditions | HubSpot documents scheduled sequences and automatic exit after a reply or meeting. Add assigned, timezone-aware activities with manual-send approval and stop-on-reply/meeting/deal rules. |
| P1 | CRM and sales boundary | ERPNext and Odoo keep lead/opportunity workflows separate from quotation/order modules. Keep RFQ/quote state independent while linking future customer and order aggregates by immutable IDs. |
| P1 | Channel and after-sales records | ITA describes representative/distributor authority and after-sales expectations for delivery, manuals, repair/replacement, and distributor support. Add explicit channel contracts, authority limits, warranty/SLA, service tasks, and complaint evidence rather than inferring commitments from email text. |

The two completed slices are described below with their evidence; the remaining rows
are design candidates for future M5 work, not claims that the current repository
already implements them. GitHub source code was used for design patterns only; no
GPL/AGPL/LGPL source was copied into this MIT upstream derivative.

## M4: Evaluation, deployment, and presentation

| Work | State | Evidence or remaining work |
| --- | --- | --- |
| M4-01 Evaluation set and protocol | Done | `fixtures/synthetic_trade_cases.jsonl` contains 64 fully synthetic records: T01-T14 regression, 30 development, and 20 holdout cases. `scripts/evaluate_trade_agent.py` validates schema and runs six deterministic checks without a model, database, or Docker. |
| M4-02 Regression and failure injection | Done | `tests/trade_agent` reports 159 passed with the disposable PostgreSQL database; T05-T09, T12, and T13 are covered by approval/export/version/concurrency tests, T10 is covered by the dedicated analytics-role permission checks, and the M5 inquiry/qualification/document/shipment slices have dedicated integration coverage. |
| M4-03 Real model evaluation | Explicitly not run | No provider credentials were used. The evaluation report defines the required numerator/denominator and error logging before any real-model run. |
| M4-04 Documentation and deployment | Done with image-build caveat | README, ADR, research notes, API contracts, evaluation protocol, Compose profiles, and analytics provisioning script are present. Docker image build could not complete because Docker Hub token retrieval timed out. |
| M4-05 Demo and contribution script | Done | `docs/demo-script.md` gives a 3-5 minute synthetic workflow and preserves the rule that unverified service claims stay deferred. |

M4 exit evidence is complete for the local synthetic path. The remaining external item is M0-01 GitHub fork authentication; it cannot be truthfully marked complete until a valid GitHub login creates the fork.

## M5-P0: Inquiry response SLA and translation review

The first M5 slice is now implemented as a separate inquiry aggregate, following the
public ITA inquiry guidance and the ERPNext/Frappe/Odoo workflow boundaries recorded in
the research notes. `inquiry_cases` stores source channel, customer role, original
language, received/due timestamps, owner, and queue state. A composite organization/RFQ
foreign key prevents orphan inquiries. Controlled `private://` attachment references
(storage ref, SHA-256, media type, filename, and optional size) are stored on the inquiry
and each append-only reply revision; binary content is outside the business database.
Reply
revisions and translation decisions are append-only, hash-bound records; the service
and API both require reviewer/admin role for translation decisions and reject author
self-review. A POST inquiry route accepts a stable external `inquiry_id`, requires an
idempotency key, and uses a PostgreSQL transaction advisory lock so concurrent first
writes replay instead of returning a uniqueness error. Queue responses derive
`overdue` at read time at the exact due boundary instead of relying on a stale stored
state. A reply revision is a draft only: because no outbound mail path exists, adding
a draft does not claim the inquiry was responded to or stop its SLA queue. The Vue
Inbox shows due/overdue state, customer role, source language, reply text, attachment
references and hashes, and review state while preserving demo mode without trusted
identity. Reply `content_hash` covers the attachment metadata as part of the immutable
customer-visible snapshot.

| Verification | Result |
| --- | --- |
| Alembic development database upgrade | Passed; head is `0013_shipment_handoff` (document consistency plus shipment handoff and freight evidence) |
| `uv run --env-file .env.example pytest tests/trade_agent -o addopts='' -q` | 159 passed against disposable PostgreSQL, including inquiry, qualification, document consistency, and shipment integration tests |
| New SLA contract/API tests | Added in `tests/trade_agent/unit/test_inquiry_sla.py` |
| `npm run typecheck` | Passed |
| `npm run build` | Passed; Vite transformed 1594 modules |
| Playwright Inbox check | Passed; desktop screenshot captured, zero browser console errors |

The dedicated PostgreSQL cases cover inquiry writes, organization isolation,
idempotency replay/conflict, concurrent-first-write serialization, attachment
round-trips, immutable triggers, the exact due-time boundary, and the
translation-review permission matrix. The remaining M5 candidates are separate
capabilities listed in the evidence-backed backlog; they are not silently counted as
implemented by this slice.

## M5-P0: Customer qualification and sanctions-screening evidence

The second M5 slice is a separate qualification aggregate. It records the customer
or inquiry target, screening source/reference, check time, result, evidence hash and
optional private attachment reference. Unresolved results (`potential_match`,
`blocked`, or `unverified`) remain visible as non-clear; only an authorized reviewer
or admin can record a `clear` result or append a final decision. Decisions are bound
to the exact evidence hash, append-only, organization-scoped, idempotent, and protected
by immutable database triggers. The inquiry foreign key prevents a screening record
from referring to another organization's inquiry. The Inbox reads these records and
shows the source, time, reviewer, hash, attachment reference, notes, and effective
result; read failure and no record never render as a pass.

| Verification | Result |
| --- | --- |
| Alembic migration | Passed; `0010_qualification_screening` on the development and disposable PostgreSQL databases |
| Qualification integration tests | 4 passed; org isolation, reviewer/admin gate, clear-result restriction, hash conflict, idempotency, FK, and immutable-trigger paths covered |
| Qualification API | GET list/detail plus POST evidence and POST final decision; identity headers, role checks, idempotency, and uniform conflict errors are enforced server-side |
| Frontend | `npm run typecheck` and `npm run build` passed; desktop/mobile Inbox screenshots checked with zero console errors and no horizontal overflow |

This is evidence capture and a blocking signal, not an automated legal determination.
The actual screening source and any country/product-specific export decision still
require an authorized human review.

## M5-P0: Shipment handoff and freight evidence

The shipment slice is implemented as an organization-scoped handoff anchored to the
exact approved quote revision and hash. It records the selected Incoterm and named
place, responsibility split, forwarder offer, carrier, insurance scope and expiry,
document-set binding, booking reference, ETA, and required export/import checks.
Evidence is append-only and hash-bound, with immutable database triggers. Readiness
is fail-closed when qualification is not clear, the document set is not ready or
consistent, insurance is missing/expired, packing or labels are unresolved, required
documents are missing, or booking/transit evidence is incomplete. Reviewer/admin gate
overrides require a non-empty reason and persist an audited immutable decision.

| Verification | Result |
| --- | --- |
| Alembic migration | Passed; head is `0013_shipment_handoff` |
| Shipment integration/API tests | 3 shipment integration cases passed; combined inquiry + shipment scope passed 11 cases |
| Full backend suite | `159 passed` against disposable PostgreSQL |
| Static checks | Ruff check and format check passed for the changed source, migrations, and tests |
| Remaining test gap | Override audit details, expired evidence, missing carrier/ETA, duplicate evidence IDs with a new idempotency key, and API override response still need dedicated regression cases |

This slice is an operational evidence gate, not a freight-forwarder, customs broker,
insurance, or legal determination. External booking, carrier APIs, and document binary
storage are not connected.
