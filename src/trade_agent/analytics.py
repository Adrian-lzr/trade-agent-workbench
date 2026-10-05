"""Restricted, explainable analytics for the trade workflow.

The analytics boundary deliberately does not accept SQL.  Callers submit a
small, validated :class:`QueryPlan`; the executor selects one of a few static
queries over database views that contain only reporting fields.  This keeps
the upstream ``DataAgentFlow`` useful for read-only questions without making
model-generated SQL an authorization boundary or a write path.

``AnalyticsQueryEngine(mode="fake")`` is the offline demonstration path.  Its
results are synthetic and the response marks them as such.  ``mode="real"``
requires an injected SQLAlchemy engine and an ``org_id`` filter on every plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session


class AnalyticsPlanError(ValueError):
    """A query plan is unsupported or cannot be authorized."""


class AnalyticsAuthorizationError(PermissionError):
    """A plan does not carry the mandatory organization boundary."""


class MetricName(StrEnum):
    """Metrics with a fixed business definition."""

    RFQ_COUNT = "rfq_count"
    QUOTE_REVISION_COUNT = "quote_revision_count"
    RUN_SUCCESS_RATE = "run_success_rate"


class DimensionName(StrEnum):
    """Dimensions exposed by the reporting views."""

    ORG_ID = "org_id"
    EVENT_DATE = "event_date"
    STATUS = "status"


class FilterName(StrEnum):
    """Filters accepted by the plan builder."""

    ORG_ID = "org_id"
    DATE_FROM = "date_from"
    DATE_TO = "date_to"
    STATUS = "status"


_ALLOWED_RUN_STATUSES = frozenset({
    "queued",
    "running",
    "waiting_input",
    "succeeded",
    "failed",
    "cancelled",
})


class AnalyticsMode(StrEnum):
    """Whether results came from the database or synthetic fixtures."""

    FAKE = "fake"
    REAL = "real"


class MetricDefinition(BaseModel):
    """Human-readable metric contract returned with every result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: MetricName
    label: str
    definition: str
    formula: str
    source_view: str


class QueryPlan(BaseModel):
    """Validated, hand-authored analytics plan.

    ``filters`` is intentionally a mapping of known names rather than a SQL
    expression.  The engine binds all values as parameters and renders only
    identifiers from its own allow-list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    metric: MetricName
    dimensions: tuple[DimensionName, ...] = ()
    filters: dict[FilterName, str | date] = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_plan(self) -> QueryPlan:
        if len(set(self.dimensions)) != len(self.dimensions):
            raise AnalyticsPlanError("dimensions must not contain duplicates")
        if len(set(self.filters)) != len(self.filters):
            raise AnalyticsPlanError("filters must not contain duplicates")
        allowed_dimensions = _METRIC_DEFINITIONS[self.metric].allowed_dimensions
        unsupported_dimensions = set(self.dimensions) - set(allowed_dimensions)
        if unsupported_dimensions:
            values = ", ".join(sorted(item.value for item in unsupported_dimensions))
            raise AnalyticsPlanError(
                f"dimensions are not allowed for {self.metric.value}: {values}"
            )
        if (
            self.metric != MetricName.RUN_SUCCESS_RATE
            and FilterName.STATUS in self.filters
        ):
            raise AnalyticsPlanError("status is only available for run_success_rate")
        org = self.filters.get(FilterName.ORG_ID)
        if org is not None and (not isinstance(org, str) or not org.strip()):
            raise AnalyticsAuthorizationError("org_id must be a non-empty string")
        _validate_date_filter(self.filters.get(FilterName.DATE_FROM), "date_from")
        _validate_date_filter(self.filters.get(FilterName.DATE_TO), "date_to")
        date_from = _as_date(self.filters.get(FilterName.DATE_FROM))
        date_to = _as_date(self.filters.get(FilterName.DATE_TO))
        if date_from is not None and date_to is not None and date_to < date_from:
            raise AnalyticsPlanError("date_to must not be earlier than date_from")
        for key, value in self.filters.items():
            if (
                key != FilterName.ORG_ID
                and isinstance(value, str)
                and not value.strip()
            ):
                raise AnalyticsPlanError(f"{key.value} must not be empty")
        status = self.filters.get(FilterName.STATUS)
        if status is not None and status not in _ALLOWED_RUN_STATUSES:
            raise AnalyticsPlanError(f"unsupported run status: {status}")
        return self


class AnalyticsPoint(BaseModel):
    """One grouped metric value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: dict[str, str | int | None]
    value: float


class AnalyticsResult(BaseModel):
    """Explainable result envelope for real and Fake execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: AnalyticsMode
    source: str
    is_synthetic: bool
    metric: MetricName
    dimensions: tuple[DimensionName, ...]
    definition: MetricDefinition
    points: tuple[AnalyticsPoint, ...]


class AnalyticsQueryRequest(BaseModel):
    """HTTP request accepting either an explicit plan or a narrow question."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plan: QueryPlan | None = None
    question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_one_input(self) -> AnalyticsQueryRequest:
        if (self.plan is None) == (self.question is None):
            raise AnalyticsPlanError("provide exactly one of plan or question")
        return self


@dataclass(frozen=True, slots=True)
class _MetricContract:
    definition: MetricDefinition
    allowed_dimensions: tuple[DimensionName, ...]
    view: str
    value_sql: str


_METRIC_DEFINITIONS: dict[MetricName, _MetricContract] = {
    MetricName.RFQ_COUNT: _MetricContract(
        MetricDefinition(
            name=MetricName.RFQ_COUNT,
            label="RFQ count",
            definition="Distinct RFQ aggregates created in the selected period.",
            formula="count(distinct rfq_id)",
            source_view="trade_agent.analytics_rfq_facts",
        ),
        (DimensionName.ORG_ID, DimensionName.EVENT_DATE),
        "trade_agent.analytics_rfq_facts",
        "count(distinct v.rfq_id)",
    ),
    MetricName.QUOTE_REVISION_COUNT: _MetricContract(
        MetricDefinition(
            name=MetricName.QUOTE_REVISION_COUNT,
            label="Quote revision count",
            definition="Immutable quote revisions created in the selected period.",
            formula="count(*)",
            source_view="trade_agent.analytics_quote_facts",
        ),
        (DimensionName.ORG_ID, DimensionName.EVENT_DATE),
        "trade_agent.analytics_quote_facts",
        "count(*)",
    ),
    MetricName.RUN_SUCCESS_RATE: _MetricContract(
        MetricDefinition(
            name=MetricName.RUN_SUCCESS_RATE,
            label="Run success rate",
            definition="Percentage of durable runs that reached succeeded status.",
            formula="100 * count(status = 'succeeded') / count(*)",
            source_view="trade_agent.analytics_run_facts",
        ),
        (DimensionName.ORG_ID, DimensionName.EVENT_DATE, DimensionName.STATUS),
        "trade_agent.analytics_run_facts",
        "100.0 * sum(case when v.status = 'succeeded' then 1 else 0 end) / nullif(count(*), 0)",
    ),
}


class AnalyticsQueryEngine:
    """Execute only allow-listed plans against authorized views or Fake data."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        mode: AnalyticsMode | str | None = None,
    ) -> None:
        if engine is not None and session_factory is not None:
            raise ValueError("pass either engine or session_factory, not both")
        configured_mode = mode or os.environ.get("TRADE_ANALYTICS_MODE", "fake")
        try:
            self.mode = AnalyticsMode(configured_mode)
        except ValueError as exc:
            raise ValueError("TRADE_ANALYTICS_MODE must be fake or real") from exc
        self._engine = engine
        self._session_factory = session_factory
        if self.mode == AnalyticsMode.REAL and self._session_factory is None:
            if self._engine is None:
                database_url = os.environ.get("TRADE_ANALYTICS_DATABASE_URL")
                if not database_url:
                    raise ValueError(
                        "real analytics requires TRADE_ANALYTICS_DATABASE_URL or an engine"
                    )
                self._engine = create_engine(database_url, pool_pre_ping=True)
            self._session_factory = sessionmaker(self._engine, expire_on_commit=False)

    def execute(self, plan: QueryPlan) -> AnalyticsResult:
        """Execute a plan, preserving the mode and metric definition in output."""
        contract = _METRIC_DEFINITIONS[plan.metric]
        org_id = plan.filters.get(FilterName.ORG_ID)
        if not isinstance(org_id, str) or not org_id.strip():
            raise AnalyticsAuthorizationError("every analytics plan requires org_id")
        if self.mode == AnalyticsMode.FAKE:
            return self._execute_fake(plan, contract)
        if self._session_factory is None:
            raise RuntimeError("real analytics session factory is not configured")
        return self._execute_real(plan, contract)

    @staticmethod
    def render_sql(plan: QueryPlan) -> tuple[str, dict[str, object]]:
        """Render the static SELECT and bound parameters for audit/testing."""
        contract = _METRIC_DEFINITIONS[plan.metric]
        org_id = plan.filters.get(FilterName.ORG_ID)
        if not isinstance(org_id, str) or not org_id.strip():
            raise AnalyticsAuthorizationError("every analytics plan requires org_id")
        columns = [f"v.{dimension.value}" for dimension in plan.dimensions]
        select_columns = f"{', '.join(columns)}, " if columns else ""
        sql = (
            f"SELECT {select_columns}{contract.value_sql} AS value "  # noqa: S608
            f"FROM {contract.view} AS v WHERE v.org_id = :org_id"
        )
        parameters: dict[str, object] = {"org_id": org_id}
        clauses = [sql]
        date_from = _as_date(plan.filters.get(FilterName.DATE_FROM))
        date_to = _as_date(plan.filters.get(FilterName.DATE_TO))
        if date_from is not None:
            clauses.append(" AND v.event_date >= :date_from")
            parameters["date_from"] = date_from
        if date_to is not None:
            clauses.append(" AND v.event_date <= :date_to")
            parameters["date_to"] = date_to
        status = plan.filters.get(FilterName.STATUS)
        if status is not None:
            clauses.append(" AND v.status = :status")
            parameters["status"] = status
        rendered = "".join(clauses)
        if plan.dimensions:
            rendered += " GROUP BY " + ", ".join(
                f"v.{dimension.value}" for dimension in plan.dimensions
            )
            rendered += " ORDER BY " + ", ".join(
                f"v.{dimension.value}" for dimension in plan.dimensions
            )
        rendered += f" LIMIT {plan.limit}"
        return rendered, parameters

    def _execute_real(
        self, plan: QueryPlan, contract: _MetricContract
    ) -> AnalyticsResult:
        sql, parameters = self.render_sql(plan)
        if not sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("analytics executor generated a non-SELECT statement")
        session_factory = self._session_factory
        if session_factory is None:
            raise RuntimeError("real analytics session factory is not configured")
        with session_factory() as session:
            rows = session.execute(text(sql), parameters).mappings().all()
        points = tuple(
            AnalyticsPoint(
                dimensions={
                    dimension.value: _serialize_dimension(row.get(dimension.value))
                    for dimension in plan.dimensions
                },
                value=float(row["value"] or 0),
            )
            for row in rows
        )
        return AnalyticsResult(
            mode=AnalyticsMode.REAL,
            source="postgres_authorized_view",
            is_synthetic=False,
            metric=plan.metric,
            dimensions=plan.dimensions,
            definition=contract.definition,
            points=points,
        )

    @staticmethod
    def _execute_fake(plan: QueryPlan, contract: _MetricContract) -> AnalyticsResult:
        org_id = str(plan.filters[FilterName.ORG_ID])
        value = {
            MetricName.RFQ_COUNT: 12.0,
            MetricName.QUOTE_REVISION_COUNT: 9.0,
            MetricName.RUN_SUCCESS_RATE: 75.0,
        }[plan.metric]
        values: list[dict[str, str | int | None]] = [{}]
        for dimension in plan.dimensions:
            if dimension == DimensionName.ORG_ID:
                for item in values:
                    item[dimension.value] = org_id
            elif dimension == DimensionName.EVENT_DATE:
                for item in values:
                    item[dimension.value] = "2026-10-01"
            elif dimension == DimensionName.STATUS:
                values = [{**item, dimension.value: "succeeded"} for item in values] + [
                    {**item, dimension.value: "failed"} for item in values
                ]
        points = tuple(
            AnalyticsPoint(
                dimensions=item,
                value=(
                    100.0
                    if item.get("status") == "succeeded"
                    else 0.0
                    if item.get("status") == "failed"
                    else value
                ),
            )
            for item in values[: plan.limit]
        )
        return AnalyticsResult(
            mode=AnalyticsMode.FAKE,
            source="fake_fixture",
            is_synthetic=True,
            metric=plan.metric,
            dimensions=plan.dimensions,
            definition=contract.definition,
            points=points,
        )


def build_query_plan(
    metric: MetricName | str,
    *,
    org_id: str,
    dimensions: Iterable[DimensionName | str] = (),
    filters: Mapping[FilterName | str, str | date] | None = None,
    limit: int = 100,
) -> QueryPlan:
    """Build a plan while making the organization boundary explicit."""
    try:
        metric_name = MetricName(metric)
    except ValueError as exc:
        raise AnalyticsPlanError(f"unsupported metric: {metric}") from exc
    normalized_dimensions = tuple(DimensionName(item) for item in dimensions)
    allowed_dimensions = _METRIC_DEFINITIONS[metric_name].allowed_dimensions
    unsupported_dimensions = set(normalized_dimensions) - set(allowed_dimensions)
    if unsupported_dimensions:
        values = ", ".join(sorted(item.value for item in unsupported_dimensions))
        raise AnalyticsPlanError(
            f"dimensions are not allowed for {metric_name.value}: {values}"
        )
    normalized_filters: dict[FilterName, str | date] = {
        FilterName(key): value for key, value in (filters or {}).items()
    }
    existing_org = normalized_filters.get(FilterName.ORG_ID)
    if existing_org is not None and existing_org != org_id:
        raise AnalyticsAuthorizationError("filter org_id does not match caller org_id")
    normalized_filters[FilterName.ORG_ID] = org_id
    return QueryPlan(
        metric=metric_name,
        dimensions=normalized_dimensions,
        filters=normalized_filters,
        limit=limit,
    )


def plan_from_question(question: str, *, org_id: str) -> QueryPlan:
    """Map a small, documented question vocabulary to a hand-authored plan.

    This is the safe adaptation boundary for ``DataAgentFlow``.  The upstream
    graph can remain a conversational front end, but it must call this mapper
    rather than execute generated SQL against trade tables.
    """
    normalized = " ".join(question.lower().split())
    if any(
        token in normalized
        for token in ("success rate", "successful runs", "run success")
    ):
        metric = MetricName.RUN_SUCCESS_RATE
    elif any(
        token in normalized
        for token in ("quote revision", "quotation revision", "quotes")
    ):
        metric = MetricName.QUOTE_REVISION_COUNT
    elif any(token in normalized for token in ("rfq", "inquir", "request for quote")):
        metric = MetricName.RFQ_COUNT
    else:
        raise AnalyticsPlanError(
            "question is outside the allow-listed analytics vocabulary; provide a QueryPlan"
        )
    dimensions = (
        (DimensionName.EVENT_DATE,)
        if any(
            token in normalized for token in ("over time", "by day", "daily", "trend")
        )
        else ()
    )
    return build_query_plan(metric, org_id=org_id, dimensions=dimensions)


class ReadOnlyDataAgentFlowAdapter:
    """Explicit adapter boundary around the upstream NL2SQL flow.

    A caller may use ``DataAgentFlow`` to classify a user question, then pass
    the resulting text here.  This adapter owns no SQL generation and exposes
    no write operation or visualization executor.
    """

    def __init__(self, engine: AnalyticsQueryEngine) -> None:
        self.engine = engine

    async def run(self, question: str, *, org_id: str) -> AnalyticsResult:
        return self.engine.execute(plan_from_question(question, org_id=org_id))


def create_analytics_router(engine: AnalyticsQueryEngine | None = None) -> APIRouter:
    """Create an optional FastAPI router for the read-only analytics boundary."""
    query_engine = engine or AnalyticsQueryEngine()
    router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

    @router.post("/query", response_model=AnalyticsResult)
    def query(
        request: AnalyticsQueryRequest,
        x_org_id: str | None = Header(default=None, alias="X-Org-Id"),
        x_role: str | None = Header(default=None, alias="X-Role"),
    ) -> AnalyticsResult:
        if not x_org_id:
            raise HTTPException(
                status_code=401, detail="analytics identity is required"
            )
        if x_role not in {"sales", "reviewer", "admin"}:
            raise HTTPException(status_code=403, detail="analytics role is not allowed")
        try:
            plan = request.plan or plan_from_question(
                request.question or "", org_id=x_org_id
            )
            plan = build_query_plan(
                plan.metric,
                org_id=x_org_id,
                dimensions=plan.dimensions,
                filters=plan.filters,
                limit=plan.limit,
            )
        except AnalyticsAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AnalyticsPlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return query_engine.execute(plan)

    return router


def _validate_date_filter(value: str | date | None, name: str) -> None:
    if value is None or isinstance(value, date):
        return
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AnalyticsPlanError(f"{name} must be an ISO date") from exc


def _as_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(value)


def _serialize_dimension(value: object) -> str | int | None:
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int)):
        return value
    return str(value)


__all__ = [
    "AnalyticsAuthorizationError",
    "AnalyticsMode",
    "AnalyticsPlanError",
    "AnalyticsPoint",
    "AnalyticsQueryEngine",
    "AnalyticsQueryRequest",
    "AnalyticsResult",
    "DimensionName",
    "FilterName",
    "MetricDefinition",
    "MetricName",
    "QueryPlan",
    "ReadOnlyDataAgentFlowAdapter",
    "build_query_plan",
    "create_analytics_router",
    "plan_from_question",
]
