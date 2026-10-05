# 3-5 Minute Demo Script

This is a speaking script for a local, synthetic-data demonstration. Say the
boundary sentence at the start: **"Every inquiry, price, customer, and amount
in this demo is synthetic; the system drafts and gates work, and a person must
approve before an external file or message."** Do not use a real customer
email or credential on screen.

The commands below are safe offline commands unless marked **M3 service**.
Do not force-stop Docker, restart Windows while Docker is running, or use a
factory reset. Exit Docker with the tray Quit action or `docker desktop stop`.

## 0:00-0:35 | Scope and reproducible setup

Show the repository and run:

```powershell
uv run python scripts/evaluate_trade_agent.py --case T01 --case T02 --case T03 --case T04 --case T11 --case T14
```

Say: "The evaluator is offline, deterministic, and reports `real model called:
False`. It proves the business gates, not model accuracy. The full fixture
file has 14 regression cases, 30 development cases, and 20 holdout cases."

## 0:35-1:15 | Complete inquiry and sourced quote

Run:

```powershell
uv run python scripts/demo_quote_draft.py --sku BOLT-M8-30-A2 --quantity 5000
```

Point out the immutable revision ID, `400.00 USD`, catalog version, and price
list source. Explain that the salesperson explicitly confirmed the SKU; the
amount came from the Decimal price rule, not from the email text or a model.
The amount is a synthetic algorithm fixture, not a commercial offer.

## 1:15-1:55 | Hard conflict and clarification

Run the evaluator for T02 and T03, or select those rows in the review UI:

```powershell
uv run python scripts/evaluate_trade_agent.py --case T02 --case T03
```

Show that carbon steel versus the A2 stainless SKU is a hard conflict with no
quote. Then show "5 boxes" without pieces per box entering clarification. Say:
"The original wording stays visible; the system does not invent a piece
count."

**M3 service:** in the API/workbench, submit the clarification as the
authenticated salesperson, resume the same `run_id`, and show the new RFQ
revision. The resume request must carry its idempotency key. Refreshing the
page must not create another revision.

## 1:55-2:45 | Review, revision, and stale approval

**M3 service:** open the quote review as the separate reviewer identity and
show the evidence, hard blockers, price source, content hash, and preview-only
state. Approve v1, then change quantity as the salesperson. Show v2 with a new
content hash and `superseded` v1; v1 approval must not unlock v2.

Open v1 in a second reviewer tab, create v2 in the first tab, then submit the
old tab. Show the 409/version-conflict response. Say: "The expected revision
is checked in the transaction; a stale screen cannot approve a newer quote."

## 2:45-3:35 | Restart recovery and safe artifact boundary

**M3 service:** pause a run at clarification or quote review, stop only the
worker process gracefully, start it again, and resume the same `run_id`. Show
contiguous Run/Event history and no duplicate RFQ or quote revision. Generate
the approved fixed-template artifact twice with one idempotency key; show one
ready artifact and one content hash. Try downloading while it is `pending` and
show the explicit refusal. The PostgreSQL/worker/export checks for this script
are covered by the current `tests/trade_agent` integration run.

## 3:35-4:20 | Read-only operating view and close

**M3 service:** use the read-only analytics identity to show the three
documented metrics (`rfq_count`, `quote_revision_count`, and
`run_success_rate`). Show that a write attempt and a source-table read are
denied. The Fake query mode must be labeled "Fake"; a real result requires the
dedicated analytics connection provisioned by
`scripts/provision_analytics_role.sql`.

Close with: "The value here is an auditable workflow: evidence, deterministic
pricing, immutable revisions, human approval, and resumable execution. The
demo does not send email, promise delivery, or report an unmeasured model
accuracy."

## Presenter checklist

- Use only `fixtures/synthetic_trade_cases.jsonl` and the synthetic catalog.
- Keep the reviewer and salesperson identities separate.
- Point at the exact revision/hash and the event history, not a generated chat
  transcript.
- If a service check is not available in the environment, say "deferred" and
  run the offline six-case evaluator instead of improvising a result.
- Preserve Docker's normal lifecycle: tray Quit or `docker desktop stop`.
