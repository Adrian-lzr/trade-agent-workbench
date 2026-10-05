from datetime import date

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from trade_agent.analytics import (
    AnalyticsAuthorizationError,
    AnalyticsMode,
    AnalyticsPlanError,
    AnalyticsQueryEngine,
    DimensionName,
    FilterName,
    MetricName,
    QueryPlan,
    ReadOnlyDataAgentFlowAdapter,
    build_query_plan,
)
from trade_agent.api import create_app


def test_fake_analytics_marks_synthetic_and_explains_three_metrics() -> None:
    engine = AnalyticsQueryEngine(mode=AnalyticsMode.FAKE)
    for metric in MetricName:
        plan = build_query_plan(
            metric,
            org_id="org-demo",
            dimensions=(DimensionName.EVENT_DATE,),
        )
        result = engine.execute(plan)
        assert result.mode == AnalyticsMode.FAKE
        assert result.is_synthetic
        assert result.source == "fake_fixture"
        assert result.definition.formula
        assert result.points


def test_plan_requires_org_and_rejects_unsupported_dimensions() -> None:
    with pytest.raises(AnalyticsAuthorizationError):
        AnalyticsQueryEngine(mode="fake").execute(
            QueryPlan(metric=MetricName.RFQ_COUNT)
        )

    with pytest.raises((AnalyticsPlanError, ValidationError)):
        build_query_plan(
            MetricName.RFQ_COUNT,
            org_id="org-demo",
            dimensions=(DimensionName.STATUS,),
        )


def test_rendered_query_is_static_select_with_bound_filters() -> None:
    engine = AnalyticsQueryEngine(mode="fake")
    plan = build_query_plan(
        MetricName.RUN_SUCCESS_RATE,
        org_id="org-demo",
        dimensions=(DimensionName.EVENT_DATE,),
        filters={
            FilterName.DATE_FROM: date(2026, 1, 1),
            FilterName.STATUS: "succeeded",
        },
    )
    sql, parameters = engine.render_sql(plan)
    assert sql.startswith("SELECT ")
    assert "analytics_run_facts" in sql
    assert "DROP" not in sql.upper()
    assert parameters["org_id"] == "org-demo"
    assert parameters["date_from"] == date(2026, 1, 1)

    with pytest.raises((AnalyticsPlanError, ValidationError)):
        build_query_plan(
            MetricName.RUN_SUCCESS_RATE,
            org_id="org-demo",
            filters={FilterName.STATUS: "DROP TABLE"},
        )


def test_real_engine_reads_only_authorized_view_and_returns_explainable_value() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS trade_agent")
        connection.execute(
            text(
                "CREATE TABLE trade_agent.rfq_source "
                "(org_id TEXT, rfq_id TEXT, event_date DATE)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO trade_agent.rfq_source VALUES "
                "('org-demo', 'rfq-1', '2026-10-01'), "
                "('org-demo', 'rfq-1', '2026-10-01'), "
                "('org-other', 'rfq-2', '2026-10-01')"
            )
        )
        connection.execute(
            text(
                "CREATE VIEW trade_agent.analytics_rfq_facts AS "
                "SELECT org_id, rfq_id, event_date FROM trade_agent.rfq_source"
            )
        )
    query_engine = AnalyticsQueryEngine(
        session_factory=sessionmaker(engine, expire_on_commit=False), mode="real"
    )
    result = query_engine.execute(
        build_query_plan(
            MetricName.RFQ_COUNT,
            org_id="org-demo",
            dimensions=(DimensionName.EVENT_DATE,),
        )
    )
    assert result.mode == AnalyticsMode.REAL
    assert not result.is_synthetic
    assert result.points[0].value == 1


@pytest.mark.asyncio
async def test_upstream_adapter_maps_question_without_exposing_sql() -> None:
    adapter = ReadOnlyDataAgentFlowAdapter(AnalyticsQueryEngine(mode="fake"))
    result = await adapter.run("Show RFQ count by day", org_id="org-demo")
    assert result.metric == MetricName.RFQ_COUNT
    assert result.dimensions == (DimensionName.EVENT_DATE,)
    with pytest.raises(AnalyticsPlanError):
        await adapter.run("delete all quote rows", org_id="org-demo")


def test_analytics_router_has_role_and_org_boundary() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/analytics/query",
        headers={"X-Org-Id": "org-demo", "X-Role": "reviewer"},
        json={
            "plan": {
                "metric": "rfq_count",
                "dimensions": ["event_date"],
                "filters": {"org_id": "org-demo"},
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "fake"
    assert response.json()["is_synthetic"] is True

    forbidden = client.post(
        "/api/v1/analytics/query",
        headers={"X-Org-Id": "org-demo", "X-Role": "unknown"},
        json={"question": "Show RFQ count"},
    )
    assert forbidden.status_code == 403

    unsupported = client.post(
        "/api/v1/analytics/query",
        headers={"X-Org-Id": "org-demo", "X-Role": "reviewer"},
        json={"question": "delete all quote rows"},
    )
    assert unsupported.status_code == 422
