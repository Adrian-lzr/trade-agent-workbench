# ADR 0001: Boundaries Between Trade Workflow and Upstream Data Agent

- **Status:** Accepted for implementation planning
- **Date:** 2026-09-29
- **Context:** Upstream baseline `9081abf1f118a18b9152b5966ce1c65c7a704baf`

## Context

The upstream project is an NL2SQL platform. `DataAgentFlow` in `src/data_agent/agent.py` classifies a question and routes it to a configured datasource agent. `create_data_agent` in `src/data_agent/graph.py` builds one NL2SQL graph. That graph generates SQL, validates it, executes it, and may run a visualization node. Its state types represent questions, SQL, query results, and responses rather than inquiries, products, quote versions, or approval decisions.

Both upstream graph entry points default to `InMemorySaver`. The default visualization executor is `LocalExecutor`, which calls Python `exec()` on the host and does not enforce its timeout. SQL validation is useful query validation, but does not by itself define business authorization or restrict database credentials. The existing Chainlit UI is an NL2SQL chat surface and requires Azure OpenAI credentials.

## Decision

1. Implement inquiry intake, extraction, clarification, product matching, deterministic pricing, quote revisions, approval, audit, and export in a separate trade domain module with its own state and graph.
2. Keep `DataAgentFlow` / `create_data_agent` for a later, explicitly read-only analytics capability. Give that capability a restricted database identity and authorized read-only views. Do not route trade writes through generated SQL.
3. Do not treat the upstream `InMemorySaver` as durable recovery. Trade workflow persistence and restart/resume behavior must be implemented and tested independently.
4. Do not expose upstream visualization code execution as a trade workflow feature by default. The local executor is unsandboxed; Azure Dynamic Sessions availability and isolation have not been verified in this baseline.
5. Treat Chainlit as an existing upstream interface only. Build the separate review workbench with Vue 3 + TypeScript + Vite and shadcn-vue components styled through Tailwind tokens. The checked npm versions on 2026-09-29 were Vue 3.5.43, TypeScript 7.0.2, Vite 8.3.1, `@vitejs/plugin-vue` 6.0.9, `vue-tsc` 3.3.11, shadcn-vue 2.8.2, and Tailwind 4.3.3. The plugin peer range includes Vite 8 and Node 22.16 satisfies its engine range. These are registry checks, not an installed frontend build; pin and verify the compatible set when M3 begins.
6. Export customer quotes as fixed-template PDF files from approved snapshots, using ReportLab (PyPI 5.0.1 checked on 2026-09-29; Python `>=3.9`, compatible with the project's Python 3.12 baseline). Do not regenerate amounts or commitments with a model during rendering; include the template version in the approved content hash as required by the plan. Verify PDF rendering and extracted text in M3.
7. Preserve upstream MIT copyright and license notices. Keep packaging discovery in view when adding modules and verify built artifacts include the intended packages.

## Consequences

- The core trade path can remain deterministic and independent of live LLM calls for pricing and approval rules.
- NL2SQL capabilities can be retained without making generated SQL a business write mechanism.
- There will be separate domain state and persistence contracts. Integration should occur at explicit analytics boundaries rather than by merging trade facts into NL2SQL `AgentState`.
- The current upstream project alone does not meet the plan's durable recovery, Fake-model local demo, or quote-review UI needs.
- Any future use of visualization execution requires an explicit security and operational review; validation of SQL does not constrain Python visualization code.

## Rejected alternatives

- **Extend the upstream `AgentState` into a trade workflow state:** its fields and graph transitions are for NL2SQL and do not represent quote revision immutability, approval identity, or trade audit history.
- **Use `DataAgentFlow` as the inquiry-to-quote orchestrator:** its routing and query execution model is not the plan's business workflow and does not provide deterministic pricing or version-bound approvals.
- **Assume SQL validation makes all downstream actions safe:** validator behavior does not replace least-privilege database access, authorization checks, or isolation for generated Python code.

## Verification limits

This decision is based on source inspection and the environment results in [upstream-baseline.md](../upstream-baseline.md). No live LLM, external datasource, Chainlit end-to-end session, Azure Dynamic Sessions executor, durable restart recovery, or trade workflow has been verified by this ADR.
