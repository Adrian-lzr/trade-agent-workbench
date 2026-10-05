from collections.abc import Mapping
import os
import sys
from typing import cast
from uuid import UUID

from langgraph.types import Command

from trade_agent.workflow import (
    TradeGraphState,
    TradeWorkflowCallbacks,
    build_trade_graph,
    trade_thread_config,
)
from trade_agent.workflow.checkpointing import open_postgres_checkpointer


def _callbacks() -> TradeWorkflowCallbacks:
    def load_rfq(state: TradeGraphState) -> Mapping[str, object]:
        return {
            "rfq_revision_id": state["rfq_revision_id"],
            "source_document_ids": ["source-doc-1"],
        }

    def validate_rfq(state: TradeGraphState) -> Mapping[str, object]:
        if state["rfq_revision_id"] == "rfq-rev-1":
            return {
                "validation_status": "needs_clarification",
                "missing_fields": ["quantity"],
            }
        return {"validation_status": "ready", "missing_fields": []}

    return TradeWorkflowCallbacks(
        load_rfq=load_rfq,
        extract_fields=lambda _state: {},
        validate_rfq=validate_rfq,
        match_catalog=lambda _state: {
            "catalog_version_id": "catalog-v1",
            "match_status": "confirmed",
            "candidate_product_ids": ["product-1"],
        },
        calculate_quote=lambda _state: {
            "price_list_version_id": "prices-v1",
            "quote_revision_id": "quote-rev-1",
            "quote_status": "ready",
        },
        quality_gate=lambda _state: {
            "quality_status": "passed",
            "quality_issue_codes": [],
        },
    )


def _interrupt_value(result: Mapping[str, object]) -> Mapping[str, object]:
    interruptions = result["__interrupt__"]
    interruption = interruptions[0]  # type: ignore[index]
    return cast("Mapping[str, object]", interruption.value)  # type: ignore[attr-defined]


def _run_phase(phase: str, run_id: str) -> None:
    database_url = os.environ["TRADE_TEST_DATABASE_URL"]
    config = trade_thread_config(run_id)
    assert config["configurable"]["thread_id"] == str(UUID(run_id))

    with open_postgres_checkpointer(database_url) as checkpointer:
        graph = build_trade_graph(_callbacks(), checkpointer=checkpointer)
        if phase == "pause":
            paused = graph.invoke(
                {
                    "run_id": run_id,
                    "rfq_id": "rfq-1",
                    "rfq_revision_id": "rfq-rev-1",
                },
                config,
            )
            assert _interrupt_value(paused) == {
                "reason": "clarification",
                "run_id": run_id,
                "rfq_id": "rfq-1",
                "rfq_revision_id": "rfq-rev-1",
                "missing_fields": ["quantity"],
            }
            assert paused["wait_reason"] == "clarification"
            assert set(paused).issubset({
                "schema_version",
                "run_id",
                "rfq_id",
                "rfq_revision_id",
                "source_document_ids",
                "wait_reason",
                "error_code",
                "error_node",
                "retry_count",
                "validation_status",
                "missing_fields",
                "__interrupt__",
            })
            return

        if phase != "resume":
            raise ValueError("phase must be pause or resume")

        checkpoint = graph.get_state(config)
        assert checkpoint.values["run_id"] == run_id
        assert checkpoint.values["rfq_revision_id"] == "rfq-rev-1"
        assert checkpoint.values["wait_reason"] == "clarification"
        assert checkpoint.next == ("wait_clarification",)

        resumed = graph.invoke(
            Command(
                resume={
                    "decision_id": "clarification-decision-1",
                    "rfq_revision_id": "rfq-rev-2",
                }
            ),
            config,
        )
        assert resumed["rfq_revision_id"] == "rfq-rev-2"
        assert _interrupt_value(resumed)["reason"] == "quote_review"


def main() -> int:
    phase, run_id = sys.argv[1:]
    _run_phase(phase, run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
