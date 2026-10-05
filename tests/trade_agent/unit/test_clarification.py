from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trade_agent.contracts import (
    CanonicalUnit,
    FactOrigin,
    FieldFact,
    RFQItem,
    RFQPlan,
    RFQRevision,
    SourceEvidence,
    SpecificationFact,
)
from trade_agent.workflow.clarification import apply_clarification_answers

CREATED_AT = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
CONFIRMED_AT = datetime(2026, 9, 29, 7, 30, tzinfo=UTC)


def _evidence(item_id: str, field: str) -> SourceEvidence:
    return SourceEvidence(
        field=field,
        source_document_id=f"doc_{item_id}",
        location="body:line:4",
        quote="10 boxes of stainless bolts, grade A2-70",
    )


def _item(
    item_id: str,
    *,
    missing_fields: tuple[str, ...] = (),
    description: str | None = None,
) -> RFQItem:
    missing = set(missing_fields)
    quantity = None if "quantity" in missing else Decimal("10")
    unit = None if "unit" in missing else CanonicalUnit.BOX
    finish = None if "finish" in missing else "plain"
    unit_raw = "boxes"
    evidence = _evidence(item_id, "quantity")
    return RFQItem(
        rfq_item_id=item_id,
        description_raw=description or "10 boxes of stainless bolts, grade A2-70",
        quantity=quantity,
        unit_raw=unit_raw,
        unit_canonical=unit,
        specifications=(SpecificationFact(name="finish", value=finish),),
        missing_fields=missing_fields,
        facts=(
            FieldFact(
                field_name="quantity",
                origin=FactOrigin.EXTRACTED,
                raw_value="10 boxes",
                normalized_value=format(quantity.normalize(), "f")
                if quantity is not None
                else None,
                evidence=(evidence,),
            ),
            FieldFact(
                field_name="unit",
                origin=FactOrigin.EXTRACTED,
                raw_value=unit_raw,
                normalized_value=unit.value if unit is not None else None,
                evidence=(evidence,),
            ),
            FieldFact(
                field_name="specifications.finish",
                origin=FactOrigin.EXTRACTED,
                raw_value=finish,
                normalized_value=finish,
                evidence=(evidence,),
            ),
        ),
        evidence=(evidence,),
    )


def _revision(*items: RFQItem) -> RFQRevision:
    plan = RFQPlan(
        rfq_revision_id="rfqr_1",
        items=items,
        facts=(
            FieldFact(
                field_name="requested_currency",
                origin=FactOrigin.EXTRACTED,
                normalized_value="USD",
            ),
            FieldFact(
                field_name="customer_id",
                origin=FactOrigin.EXTRACTED,
                normalized_value=None,
            ),
            FieldFact(
                field_name="trade_term",
                origin=FactOrigin.EXTRACTED,
                normalized_value=None,
            ),
            FieldFact(
                field_name="named_place",
                origin=FactOrigin.EXTRACTED,
                normalized_value=None,
            ),
            FieldFact(
                field_name="requested_delivery_date",
                origin=FactOrigin.EXTRACTED,
                normalized_value=None,
            ),
        ),
    )
    return RFQRevision(
        org_id="org_1",
        rfq_id="rfq_1",
        revision_id="rfqr_1",
        revision_no=1,
        plan=plan,
        created_at=CREATED_AT,
        created_by="sales_1",
    )


def _clarify(
    revision: RFQRevision,
    answers: dict[str, dict[str, object]],
) -> RFQRevision:
    return apply_clarification_answers(
        revision,
        answers,
        actor_id="sales_2",
        revision_id="rfqr_2",
        revision_no=2,
        created_at=CONFIRMED_AT,
    )


def test_applies_answers_across_items_and_records_user_provenance() -> None:
    first = _item("item_1", missing_fields=("unit",))
    second = _item("item_2", missing_fields=("finish",))
    untouched = _item("item_3", missing_fields=("quantity",))
    original = _revision(first, second, untouched)
    original_snapshot = original.model_dump(mode="python")

    revised = _clarify(
        original,
        {"item_1": {"unit": "box"}, "item_2": {"finish": "powder coated"}},
    )

    assert revised.revision_id == "rfqr_2"
    assert revised.revision_no == 2
    assert revised.parent_revision_id == original.revision_id
    assert revised.plan.rfq_revision_id == revised.revision_id
    assert [item.rfq_item_id for item in revised.plan.items] == [
        "item_1",
        "item_2",
        "item_3",
    ]
    resolved_unit, resolved_finish, still_missing = revised.plan.items
    assert resolved_unit.description_raw == first.description_raw
    assert resolved_unit.evidence == first.evidence
    assert resolved_unit.unit_canonical == CanonicalUnit.BOX
    assert resolved_unit.unit_raw == "box"
    assert resolved_unit.quantity == first.quantity == Decimal("10")
    assert "unit" not in resolved_unit.missing_fields
    unit_fact = next(fact for fact in resolved_unit.facts if fact.field_name == "unit")
    assert unit_fact.origin is FactOrigin.USER_CONFIRMED
    assert unit_fact.normalized_value == "box"
    assert unit_fact.confirmed_by == "sales_2"
    assert unit_fact.confirmed_at == CONFIRMED_AT
    assert unit_fact.evidence == first.facts[1].evidence
    assert resolved_finish.specifications[0].value == "powder coated"
    finish_fact = next(
        fact
        for fact in resolved_finish.facts
        if fact.field_name == "specifications.finish"
    )
    assert finish_fact.origin is FactOrigin.USER_CONFIRMED
    assert finish_fact.raw_value == "powder coated"
    assert finish_fact.normalized_value == "powder coated"
    assert finish_fact.confirmed_by == "sales_2"
    assert finish_fact.confirmed_at == CONFIRMED_AT
    assert resolved_finish.missing_fields == ()
    assert still_missing == untouched
    assert original.model_dump(mode="python") == original_snapshot


def test_partial_answer_keeps_unanswered_missing_fields() -> None:
    original = _revision(_item("item_1", missing_fields=("quantity", "unit")))

    revised = _clarify(original, {"item_1": {"quantity": "12.5"}})

    item = revised.plan.items[0]
    assert item.quantity == Decimal("12.5")
    assert item.missing_fields == ("unit",)
    quantity_fact = next(fact for fact in item.facts if fact.field_name == "quantity")
    assert quantity_fact.origin is FactOrigin.USER_CONFIRMED
    assert quantity_fact.raw_value == "12.5"
    assert quantity_fact.normalized_value == "12.5"


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"item_1": {"finish": "plain"}}, "not missing"),
        ({"item_1": {"unknown": "value"}}, "not missing"),
        ({"unknown_item": {"quantity": 2}}, "unknown RFQ items"),
        ({"item_1": {"quantity": None}}, "quantity answer"),
        ({"item_1": {"quantity": "  "}}, "finite positive number"),
        ({"item_1": {}}, "field answers"),
    ],
)
def test_rejects_non_missing_unknown_or_empty_answers(
    answers: dict[str, dict[str, object]], message: str
) -> None:
    missing = (
        ("quantity", "unit") if "quantity" in answers.get("item_1", {}) else ("unit",)
    )
    original = _revision(_item("item_1", missing_fields=missing))

    with pytest.raises(ValueError, match=message):
        _clarify(original, answers)


def test_rejects_unknown_field_even_if_listed_as_missing() -> None:
    item = _item("item_1", missing_fields=("unit", "unknown"))
    original = _revision(item)

    with pytest.raises(ValueError, match="unknown missing fields"):
        _clarify(original, {"item_1": {"unknown": "value"}})


def test_does_not_infer_a_unit_from_ambiguous_source_text() -> None:
    original = _revision(_item("item_1", missing_fields=("quantity", "unit")))

    revised = _clarify(original, {"item_1": {"quantity": 10}})

    item = revised.plan.items[0]
    assert item.description_raw == "10 boxes of stainless bolts, grade A2-70"
    assert item.quantity == Decimal("10")
    assert item.unit_raw == "boxes"
    assert item.unit_canonical is None
    assert "unit" in item.missing_fields


@pytest.mark.parametrize("answer", ["boxes of 100", "pcs", "unknown"])
def test_rejects_unit_answers_without_a_supported_canonical_unit(answer: str) -> None:
    original = _revision(_item("item_1", missing_fields=("unit",)))

    with pytest.raises(ValueError, match="supported canonical unit"):
        _clarify(original, {"item_1": {"unit": answer}})
