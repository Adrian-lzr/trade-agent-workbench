from collections.abc import Mapping
from typing import cast
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import pytest

from trade_agent.workflow import (
    TradeGraphState,
    TradeWorkflowCallbacks,
    WorkflowFailureError,
    build_trade_graph,
    trade_thread_config,
)

RUN_ID = str(uuid4())
INITIAL_STATE: TradeGraphState = {
    "run_id": RUN_ID,
    "rfq_id": "rfq-1",
    "rfq_revision_id": "rfq-rev-1",
}


def _callbacks(**overrides: object) -> TradeWorkflowCallbacks:
    callbacks = {
        "load_rfq": lambda state: {
            "rfq_revision_id": state["rfq_revision_id"],
            "source_document_ids": ["doc-1"],
        },
        "extract_fields": lambda _state: {},
        "validate_rfq": lambda _state: {"validation_status": "ready"},
        "match_catalog": lambda _state: {
            "catalog_version_id": "catalog-v1",
            "match_status": "confirmed",
            "candidate_product_ids": ["product-1"],
        },
        "calculate_quote": lambda _state: {
            "price_list_version_id": "prices-v1",
            "quote_revision_id": "quote-rev-1",
            "quote_status": "ready",
        },
        "quality_gate": lambda _state: {
            "quality_status": "passed",
            "quality_issue_codes": [],
        },
    }
    callbacks.update(overrides)
    return TradeWorkflowCallbacks(**callbacks)


def _graph(callbacks: TradeWorkflowCallbacks | None = None):
    return build_trade_graph(
        callbacks or _callbacks(),
        checkpointer=InMemorySaver(),
    )


def _interrupt(result: Mapping[str, object]) -> Mapping[str, object]:
    interruptions = result["__interrupt__"]
    interruption = interruptions[0]  # type: ignore[index]
    return cast("Mapping[str, object]", interruption.value)  # type: ignore[attr-defined]


def test_success_path_interrupts_for_quote_review_with_id_only_state() -> None:
    graph = _graph()
    config = trade_thread_config(RUN_ID)
    result = graph.invoke(INITIAL_STATE, config)

    assert _interrupt(result) == {
        "reason": "quote_review",
        "run_id": RUN_ID,
        "rfq_revision_id": "rfq-rev-1",
        "quote_revision_id": "quote-rev-1",
    }
    assert result["wait_reason"] == "quote_review"
    assert "items" not in result
    assert "__interrupt__" in result

    resumed = graph.invoke(Command(resume={"decision_id": "review-decision-1"}), config)

    assert resumed["quote_review_decision_id"] == "review-decision-1"
    assert "approved" not in resumed
    assert "__interrupt__" not in resumed


def test_clarification_wait_resumes_with_new_revision_and_reloads_it() -> None:
    loaded_revisions: list[str] = []

    def load_rfq(state: TradeGraphState) -> Mapping[str, object]:
        loaded_revisions.append(state["rfq_revision_id"])
        return {
            "rfq_revision_id": state["rfq_revision_id"],
            "source_document_ids": ["doc-1"],
        }

    def validate_rfq(state: TradeGraphState) -> Mapping[str, object]:
        if state["rfq_revision_id"] == "rfq-rev-1":
            return {
                "validation_status": "needs_clarification",
                "missing_fields": ["quantity"],
            }
        return {"validation_status": "ready", "missing_fields": []}

    callbacks = _callbacks(load_rfq=load_rfq, validate_rfq=validate_rfq)
    graph = _graph(callbacks)
    config = trade_thread_config(RUN_ID)
    paused = graph.invoke(INITIAL_STATE, config)

    assert _interrupt(paused)["reason"] == "clarification"
    assert paused["wait_reason"] == "clarification"
    assert _interrupt(paused)["missing_fields"] == ["quantity"]

    resumed = graph.invoke(
        Command(
            resume={"decision_id": "clarification-1", "rfq_revision_id": "rfq-rev-2"}
        ),
        config,
    )

    assert loaded_revisions == ["rfq-rev-1", "rfq-rev-2"]
    assert resumed["rfq_revision_id"] == "rfq-rev-2"
    assert _interrupt(resumed)["reason"] == "quote_review"


def test_match_wait_resumes_with_decision_id_then_routes_to_quote() -> None:
    observed_decisions: list[list[str]] = []

    def match_catalog(state: TradeGraphState) -> Mapping[str, object]:
        decisions = list(state.get("match_decision_ids", []))
        observed_decisions.append(decisions)
        status = "confirmed" if decisions else "needs_review"
        return {
            "catalog_version_id": "catalog-v1",
            "match_status": status,
            "candidate_product_ids": ["product-1", "product-2"],
        }

    graph = _graph(_callbacks(match_catalog=match_catalog))
    config = trade_thread_config(RUN_ID)
    paused = graph.invoke(INITIAL_STATE, config)

    assert _interrupt(paused)["reason"] == "product_selection"
    assert paused["wait_reason"] == "product_selection"
    assert _interrupt(paused)["candidate_product_ids"] == ["product-1", "product-2"]

    resumed = graph.invoke(
        Command(resume={"decision_id": "match-decision-1"}),
        config,
    )

    assert observed_decisions == [[], ["match-decision-1"]]
    assert resumed["match_decision_ids"] == ["match-decision-1"]
    assert _interrupt(resumed)["reason"] == "quote_review"


def test_conflict_and_callback_failure_end_with_stable_error_reasons() -> None:
    conflict = _graph(
        _callbacks(
            validate_rfq=lambda _state: {
                "validation_status": "conflict",
                "conflict_fields": ["material"],
            }
        )
    ).invoke(INITIAL_STATE, trade_thread_config(RUN_ID))

    assert conflict["error_code"] == "rfq.hard_conflict"
    assert conflict["error_node"] == "validate_rfq"
    assert "__interrupt__" not in conflict

    def not_found(_state: TradeGraphState) -> Mapping[str, object]:
        raise WorkflowFailureError("rfq.not_found")

    missing = _graph(_callbacks(load_rfq=not_found)).invoke(
        INITIAL_STATE, trade_thread_config(str(uuid4()))
    )

    assert missing["error_code"] == "rfq.not_found"
    assert missing["error_node"] == "load_rfq"


def test_quality_gate_blocker_has_error_route_and_reason() -> None:
    result = _graph(
        _callbacks(
            quality_gate=lambda _state: {
                "quality_status": "blocked",
                "quality_issue_codes": ["missing_price_source"],
            }
        )
    ).invoke(INITIAL_STATE, trade_thread_config(RUN_ID))

    assert result["error_code"] == "quote.quality_gate_blocked"
    assert result["error_node"] == "quality_gate"
    assert result["quality_issue_codes"] == ["missing_price_source"]
    assert "__interrupt__" not in result


def test_invalid_callback_output_is_a_routed_error() -> None:
    result = _graph(
        _callbacks(match_catalog=lambda _state: {"product": {"large": "object"}})
    ).invoke(INITIAL_STATE, trade_thread_config(RUN_ID))

    assert result["error_code"] == "workflow.invalid_node_output"
    assert result["error_node"] == "match_catalog"


def test_missing_routing_status_is_a_routed_error() -> None:
    result = _graph(
        _callbacks(validate_rfq=lambda _state: {"missing_fields": ["quantity"]})
    ).invoke(INITIAL_STATE, trade_thread_config(RUN_ID))

    assert result["error_code"] == "workflow.invalid_node_output"
    assert result["error_node"] == "validate_rfq"


def test_wait_routes_require_actionable_context() -> None:
    missing_fields = _graph(
        _callbacks(
            validate_rfq=lambda _state: {
                "validation_status": "needs_clarification",
                "missing_fields": [],
            }
        )
    ).invoke(INITIAL_STATE, trade_thread_config(RUN_ID))
    missing_candidates = _graph(
        _callbacks(
            match_catalog=lambda _state: {
                "catalog_version_id": "catalog-v1",
                "match_status": "needs_review",
                "candidate_product_ids": [],
            }
        )
    ).invoke(INITIAL_STATE, trade_thread_config(str(uuid4())))

    assert missing_fields["error_code"] == "workflow.invalid_node_output"
    assert missing_fields["error_node"] == "validate_rfq"
    assert missing_candidates["error_code"] == "workflow.invalid_node_output"
    assert missing_candidates["error_node"] == "match_catalog"


def test_start_state_and_thread_config_require_run_uuid() -> None:
    invalid = _graph().invoke(
        {"run_id": "not-a-uuid", "rfq_id": "rfq-1", "rfq_revision_id": "rev-1"},
        trade_thread_config(RUN_ID),
    )

    assert invalid["error_code"] == "workflow.invalid_start_state"
    with pytest.raises(ValueError, match="run_id must be a UUID"):
        trade_thread_config("not-a-uuid")


def test_workflow_failure_rejects_non_identifier_error_codes() -> None:
    with pytest.raises(ValueError, match="stable identifier"):
        WorkflowFailureError("sensitive customer error details")
