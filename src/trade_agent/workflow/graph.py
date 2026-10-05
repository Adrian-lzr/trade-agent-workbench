"""Dependency-injected LangGraph workflow for RFQ routing and review waits."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from trade_agent.workflow.state import TradeGraphState

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

NodeCallback = Callable[[TradeGraphState], Mapping[str, object]]
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_MAX_ID_LENGTH = 128
_MAX_LIST_LENGTH = 50
_ID_FIELDS = frozenset({
    "rfq_revision_id",
    "catalog_version_id",
    "price_list_version_id",
    "quote_revision_id",
})
_LIST_FIELDS = frozenset({
    "source_document_ids",
    "candidate_product_ids",
    "missing_fields",
    "conflict_fields",
    "quality_issue_codes",
})
_STATUS_VALUES = {
    "validation_status": frozenset({"ready", "needs_clarification", "conflict"}),
    "match_status": frozenset({"confirmed", "needs_review", "no_safe_match"}),
    "quote_status": frozenset({"ready", "blocked"}),
    "quality_status": frozenset({"passed", "blocked"}),
}
_REQUIRED_OUTPUTS = {
    "load_rfq": frozenset({"rfq_revision_id"}),
    "extract_fields": frozenset(),
    "validate_rfq": frozenset({"validation_status"}),
    "match_catalog": frozenset({"catalog_version_id", "match_status"}),
    "calculate_quote": frozenset({"quote_status"}),
    "quality_gate": frozenset({"quality_status"}),
}
_INITIAL_FIELDS = frozenset({"schema_version", "run_id", "rfq_id", "rfq_revision_id"})


class WorkflowFailureError(Exception):
    """A callback failure with a safe, stable code for graph state."""

    def __init__(self, code: str) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("workflow error code must be a stable identifier")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TradeWorkflowCallbacks:
    """Business operations supplied by the application or deterministic tests."""

    load_rfq: NodeCallback
    extract_fields: NodeCallback
    validate_rfq: NodeCallback
    match_catalog: NodeCallback
    calculate_quote: NodeCallback
    quality_gate: NodeCallback


_NODE_OUTPUTS: dict[str, frozenset[str]] = {
    "load_rfq": frozenset({"rfq_revision_id", "source_document_ids"}),
    "extract_fields": frozenset(),
    "validate_rfq": frozenset({
        "validation_status",
        "missing_fields",
        "conflict_fields",
    }),
    "match_catalog": frozenset({
        "catalog_version_id",
        "match_status",
        "candidate_product_ids",
    }),
    "calculate_quote": frozenset({
        "price_list_version_id",
        "quote_revision_id",
        "quote_status",
    }),
    "quality_gate": frozenset({"quality_status", "quality_issue_codes"}),
}


def _stable_id(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value.strip()) and len(value) <= _MAX_ID_LENGTH
    )


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= _MAX_LIST_LENGTH
        and all(_stable_id(item) for item in value)
    )


def _valid_output_value(key: str, value: object) -> bool:
    if key in _ID_FIELDS:
        return _stable_id(value)
    if key in _LIST_FIELDS:
        return _string_list(value)
    allowed_values = _STATUS_VALUES.get(key)
    return allowed_values is None or value in allowed_values


def _valid_validation_context(update: Mapping[str, object]) -> bool:
    status = update["validation_status"]
    missing_fields = update.get("missing_fields", [])
    conflict_fields = update.get("conflict_fields", [])
    return (
        (
            status == "needs_clarification"
            and bool(missing_fields)
            and not conflict_fields
        )
        or (status == "conflict" and bool(conflict_fields))
        or (status == "ready" and not missing_fields and not conflict_fields)
    )


def _valid_callback_update(node_name: str, update: object) -> bool:
    if (
        not isinstance(update, Mapping)
        or set(update) - _NODE_OUTPUTS[node_name]
        or not _REQUIRED_OUTPUTS[node_name].issubset(update)
        or not all(_valid_output_value(key, value) for key, value in update.items())
    ):
        return False
    if node_name == "validate_rfq" and not _valid_validation_context(update):
        return False
    if (
        node_name == "match_catalog"
        and update["match_status"] == "needs_review"
        and not update.get("candidate_product_ids", [])
    ):
        return False
    return (
        node_name != "calculate_quote"
        or update.get("quote_status") != "ready"
        or {"price_list_version_id", "quote_revision_id"}.issubset(update)
    )


def _node_error(node_name: str, code: str) -> dict[str, object]:
    return {"error_code": code, "error_node": node_name, "wait_reason": None}


def _invoke_callback(
    node_name: str, callback: NodeCallback, state: TradeGraphState
) -> Mapping[str, object] | dict[str, object]:
    try:
        return callback(cast("TradeGraphState", dict(state)))
    except WorkflowFailureError as exc:
        return _node_error(node_name, exc.code)
    except Exception:  # noqa: BLE001 - unexpected callback failures get a safe code
        return _node_error(node_name, "workflow.node_failed")


def _route_update(node_name: str, update: Mapping[str, object]) -> dict[str, object]:
    result = dict(update)
    if node_name == "validate_rfq":
        status = result["validation_status"]
        if status == "conflict":
            return {**result, **_node_error(node_name, "rfq.hard_conflict")}
        result["wait_reason"] = (
            "clarification" if status == "needs_clarification" else None
        )
    elif node_name == "match_catalog":
        status = result["match_status"]
        result["wait_reason"] = "product_selection" if status != "confirmed" else None
    elif node_name == "calculate_quote" and result["quote_status"] == "blocked":
        return {**result, **_node_error(node_name, "quote.blocked")}
    elif node_name == "quality_gate":
        if result["quality_status"] == "blocked":
            return {
                **result,
                **_node_error(node_name, "quote.quality_gate_blocked"),
            }
        result["wait_reason"] = "quote_review"
    return result


def _run_callback(
    node_name: str, callback: NodeCallback
) -> Callable[[TradeGraphState], dict[str, object]]:
    def run(state: TradeGraphState) -> dict[str, object]:
        update = _invoke_callback(node_name, callback, state)
        if "error_code" in update:
            return dict(update)
        if not _valid_callback_update(node_name, update):
            return _node_error(node_name, "workflow.invalid_node_output")
        if (
            node_name == "load_rfq"
            and update["rfq_revision_id"] != state["rfq_revision_id"]
        ):
            return _node_error(node_name, "rfq.revision_mismatch")
        return _route_update(node_name, update)

    return run


def _validate_start(state: TradeGraphState) -> dict[str, object]:
    run_id = state.get("run_id")
    rfq_id = state.get("rfq_id")
    revision_id = state.get("rfq_revision_id")
    try:
        normalized_run_id = str(UUID(run_id)) if isinstance(run_id, str) else ""
    except ValueError:
        normalized_run_id = ""
    if (
        set(state) - _INITIAL_FIELDS
        or state.get("schema_version", 1) != 1
        or not normalized_run_id
        or not _stable_id(rfq_id)
        or not _stable_id(revision_id)
    ):
        return _node_error("validate_start", "workflow.invalid_start_state")
    return {
        "schema_version": 1,
        "run_id": normalized_run_id,
        "retry_count": 0,
        "wait_reason": None,
        "error_code": None,
        "error_node": None,
    }


def _route_start(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "load_rfq"


def _route_after_node(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "continue"


def _route_validation(state: TradeGraphState) -> str:
    if state.get("error_code"):
        return "error"
    return "wait" if state.get("wait_reason") == "clarification" else "match"


def _route_match(state: TradeGraphState) -> str:
    if state.get("error_code"):
        return "error"
    return "quote" if state.get("match_status") == "confirmed" else "wait"


def _route_quality(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "wait"


def _resume_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    decision_id = value.get("decision_id")
    return value if _stable_id(decision_id) else None


def _wait_clarification(state: TradeGraphState) -> dict[str, object]:
    value = interrupt({
        "reason": "clarification",
        "run_id": state["run_id"],
        "rfq_id": state["rfq_id"],
        "rfq_revision_id": state["rfq_revision_id"],
        "missing_fields": state.get("missing_fields", []),
    })
    decision = _resume_mapping(value)
    revision_id = decision.get("rfq_revision_id") if decision else None
    if decision is None or not _stable_id(revision_id):
        return {
            "error_code": "workflow.invalid_resume",
            "error_node": "wait_clarification",
            "wait_reason": None,
        }
    return {"rfq_revision_id": revision_id, "wait_reason": None}


def _wait_match_review(state: TradeGraphState) -> dict[str, object]:
    value = interrupt({
        "reason": "product_selection",
        "run_id": state["run_id"],
        "rfq_revision_id": state["rfq_revision_id"],
        "candidate_product_ids": state.get("candidate_product_ids", []),
    })
    decision = _resume_mapping(value)
    decision_id = decision.get("decision_id") if decision else None
    if decision is None or not _stable_id(decision_id):
        return {
            "error_code": "workflow.invalid_resume",
            "error_node": "wait_match_review",
            "wait_reason": None,
        }
    existing = state.get("match_decision_ids", [])
    return {
        "match_decision_ids": [*existing, decision_id],
        "wait_reason": None,
    }


def _wait_quote_review(state: TradeGraphState) -> dict[str, object]:
    value = interrupt({
        "reason": "quote_review",
        "run_id": state["run_id"],
        "rfq_revision_id": state["rfq_revision_id"],
        "quote_revision_id": state["quote_revision_id"],
    })
    decision = _resume_mapping(value)
    decision_id = decision.get("decision_id") if decision else None
    if decision is None or not _stable_id(decision_id):
        return {
            "error_code": "workflow.invalid_resume",
            "error_node": "wait_quote_review",
            "wait_reason": None,
        }
    return {
        "quote_review_decision_id": decision_id,
        "wait_reason": None,
    }


def _route_after_clarification_wait(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "load_rfq"


def _route_after_match_wait(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "match_catalog"


def _route_after_quote_wait(state: TradeGraphState) -> str:
    return "error" if state.get("error_code") else "done"


def _terminal_error(_state: TradeGraphState) -> dict[str, object]:
    return {}


def build_trade_graph(
    callbacks: TradeWorkflowCallbacks,
    *,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Compile the RFQ workflow with caller-owned callbacks and persistence."""
    graph = StateGraph(TradeGraphState)
    graph.add_node("validate_start", _validate_start)
    graph.add_node("load_rfq", _run_callback("load_rfq", callbacks.load_rfq))
    graph.add_node(
        "extract_fields", _run_callback("extract_fields", callbacks.extract_fields)
    )
    graph.add_node(
        "validate_rfq", _run_callback("validate_rfq", callbacks.validate_rfq)
    )
    graph.add_node("wait_clarification", _wait_clarification)
    graph.add_node(
        "match_catalog", _run_callback("match_catalog", callbacks.match_catalog)
    )
    graph.add_node("wait_match_review", _wait_match_review)
    graph.add_node(
        "calculate_quote", _run_callback("calculate_quote", callbacks.calculate_quote)
    )
    graph.add_node(
        "quality_gate", _run_callback("quality_gate", callbacks.quality_gate)
    )
    graph.add_node("wait_quote_review", _wait_quote_review)
    graph.add_node("terminal_error", _terminal_error)

    graph.add_edge(START, "validate_start")
    graph.add_conditional_edges(
        "validate_start",
        _route_start,
        {"load_rfq": "load_rfq", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "load_rfq",
        _route_after_node,
        {"continue": "extract_fields", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "extract_fields",
        _route_after_node,
        {"continue": "validate_rfq", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "validate_rfq",
        _route_validation,
        {
            "match": "match_catalog",
            "wait": "wait_clarification",
            "error": "terminal_error",
        },
    )
    graph.add_conditional_edges(
        "wait_clarification",
        _route_after_clarification_wait,
        {"load_rfq": "load_rfq", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "match_catalog",
        _route_match,
        {
            "quote": "calculate_quote",
            "wait": "wait_match_review",
            "error": "terminal_error",
        },
    )
    graph.add_conditional_edges(
        "wait_match_review",
        _route_after_match_wait,
        {"match_catalog": "match_catalog", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "calculate_quote",
        _route_after_node,
        {"continue": "quality_gate", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "quality_gate",
        _route_quality,
        {"wait": "wait_quote_review", "error": "terminal_error"},
    )
    graph.add_conditional_edges(
        "wait_quote_review",
        _route_after_quote_wait,
        {"done": END, "error": "terminal_error"},
    )
    graph.add_edge("terminal_error", END)
    return graph.compile(checkpointer=checkpointer)


def trade_thread_config(run_id: str) -> RunnableConfig:
    """Build the stable LangGraph thread config from the run's UUID."""
    try:
        thread_id = str(UUID(run_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("run_id must be a UUID") from exc
    return {"configurable": {"thread_id": thread_id}}
