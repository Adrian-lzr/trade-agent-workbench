# Synthetic evaluation fixtures

This directory contains **synthetic data only**. The names, email text, SKU
values, prices, dates, and quantities are generated for regression testing and
have no commercial value. Do not copy a fixture into a customer quotation.

## File and split

`synthetic_trade_cases.jsonl` is newline-delimited JSON. Every line is one
case with this shape:

```json
{
  "case_id": "T01",
  "split": "regression",
  "synthetic_data": true,
  "source_document_id": "doc-t01",
  "source_text": "...",
  "purpose": "complete quote",
  "labels": {
    "fields": {
      "quantity": {"value": "5000", "evidence": ["body:line:1"]},
      "unit": {"value": "piece", "evidence": ["body:line:1"]}
    },
    "accepted_skus": ["BOLT-M8-30-A2"],
    "clarification_questions": [],
    "blockers": [],
    "expected_outcome": "quote_ready",
    "test_refs": ["T01"]
  }
}
```

The `regression` split is the hand-authored T01-T14 acceptance matrix from
the master plan. `development` contains 30 cases for prompt/rule iteration;
`holdout` contains 20 cases that must not be used to tune a prompt or a
deterministic rule. The holdout is deliberately small and synthetic: it is a
workflow regression sample, not a claim about real-world model accuracy.

Each field annotation records a normalized value and one or more source
locations. A missing value is represented by `null` plus a clarification label;
the raw source sentence is retained in `source_text`. `accepted_skus` lists
products that satisfy hard specifications. `blockers` lists the expected
business gate names. For T05-T10 and T12-T13, the label is an operation and
`test_refs` points to the API/PostgreSQL tests; the offline evaluator reports
those cases as deferred rather than pretending to run a database transaction.

The development and holdout examples cover direct SKU references, multiple
lines, quantity and packaging ambiguity, spelling and unit wording variants,
missing material, hard material conflicts, unsupported currencies, expired
quoted prices, certificate requests without evidence, unconfirmed delivery
claims, customer references to an old quote, and prompt-injection text.

## Running the controlled evaluator

From the repository root:

```powershell
uv run python scripts/evaluate_trade_agent.py
uv run python scripts/evaluate_trade_agent.py --json-out build/evaluation.json
uv run python scripts/evaluate_trade_agent.py --require-complete  # requires integration evidence
```

The default command is offline and deterministic. It executes only the
deterministic quote/matching and reply-fact checks (T01, T02, T03, T04, T11,
and T14), validates the JSONL schema and split counts, and lists the database
or API cases as deferred. It never calls a model provider, opens Docker, or
changes a database. `--require-complete` is a CI gate for a run that has
already collected the linked integration evidence; it fails if any deferred
case remains.

No real customer data, credentials, provider response, or model-generated
label is stored here.
