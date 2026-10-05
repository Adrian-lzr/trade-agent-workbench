from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError
import pytest

from trade_agent.contracts import (
    CanonicalUnit,
    FactOrigin,
    FieldFact,
    ProductSnapshot,
    QuoteLineSnapshot,
    QuoteRevision,
    RFQItem,
    RFQPlan,
    RFQRevision,
    SourceEvidence,
    SpecificationFact,
)

CREATED_AT = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


def _evidence(field: str) -> SourceEvidence:
    return SourceEvidence(
        field=field,
        source_document_id="doc_demo_01",
        location="body:line:2",
        quote="5000 pcs stainless bolts M8 x 30 A2-70",
    )


def _fact(field: str, normalized: str | None, raw: str | None = None) -> FieldFact:
    return FieldFact(
        field_name=field,
        origin=FactOrigin.EXTRACTED,
        raw_value=raw,
        normalized_value=normalized,
        evidence=(_evidence(field),),
    )


def _rfq_item(**overrides: object) -> RFQItem:
    values: dict[str, object] = {
        "rfq_item_id": "item_001",
        "description_raw": "stainless bolts M8 x 30 A2-70, 5000 pcs",
        "quantity": Decimal("5000"),
        "unit_raw": "pcs",
        "unit_canonical": CanonicalUnit.PIECE,
        "specifications": (SpecificationFact(name="material_grade", value="A2-70"),),
        "facts": (
            _fact("quantity", "5000", "5000 pcs"),
            _fact("unit", "piece", "pcs"),
            _fact("specifications.material_grade", "A2-70", "A2-70"),
        ),
        "evidence": (_evidence("quantity"),),
    }
    values.update(overrides)
    return RFQItem(**values)


def _plan() -> RFQPlan:
    fields = (
        ("requested_currency", "USD"),
        ("customer_id", None),
        ("trade_term", None),
        ("named_place", None),
        ("requested_delivery_date", None),
    )
    return RFQPlan(
        rfq_revision_id="rfqr_demo_01",
        items=(_rfq_item(),),
        facts=tuple(_fact(name, value) for name, value in fields),
    )


def _quote_revision() -> QuoteRevision:
    line = QuoteLineSnapshot(
        quote_item_id="quote-item-001",
        rfq_item_id="item_001",
        product=ProductSnapshot(
            sku="BOLT-M8-30-A2",
            catalog_version="SYNTH-CAT-1",
            name="Hex bolt M8 x 30 A2-70",
            specifications=(SpecificationFact(name="material_grade", value="A2-70"),),
        ),
        quantity=Decimal("5000"),
        unit=CanonicalUnit.PIECE,
        price_list_version="SYNTH-USD-2026-01",
        price_list_source="Synthetic demo data; no commercial value",
        unit_price=Decimal("0.08"),
        price_unit=CanonicalUnit.PIECE,
        line_amount=Decimal("400.00"),
    )
    return QuoteRevision(
        org_id="org_demo_01",
        quotation_id="quote_demo_01",
        revision_id="quote_rev_demo_01",
        revision_no=1,
        rfq_revision_id="rfqr_demo_01",
        catalog_version="SYNTH-CAT-1",
        price_list_version="SYNTH-USD-2026-01",
        items=(line,),
        trade_term="FOB",
        named_place="Shanghai",
        valid_until=None,
        response_body="Please review the attached draft quotation.",
        template_version="quote-v1",
        created_at=CREATED_AT,
        created_by="sales_demo_01",
    )


def test_rfq_contracts_keep_normalized_facts_and_provenance_immutable() -> None:
    item = _rfq_item()
    revision = RFQRevision(
        org_id="org_demo_01",
        rfq_id="rfq_demo_01",
        revision_id="rfqr_demo_01",
        revision_no=1,
        plan=_plan(),
        created_at=CREATED_AT,
        created_by="sales_demo_01",
    )

    assert item.quantity == Decimal("5000")
    assert (
        revision.plan.items[0].facts[0].evidence[0].source_document_id == "doc_demo_01"
    )
    with pytest.raises(ValidationError, match="frozen"):
        item.quantity = Decimal("6000")


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_rfq_item_rejects_non_positive_or_non_finite_quantity(
    quantity: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        _rfq_item(quantity=quantity)


def test_rfq_item_requires_explicit_missing_unit_and_matching_provenance() -> None:
    unknown_unit_facts = (
        _fact("quantity", "5000", "5000 pcs"),
        _fact("unit", None, "cartons"),
        _fact("specifications.material_grade", "A2-70", "A2-70"),
    )
    item = _rfq_item(
        unit_raw="cartons",
        unit_canonical=None,
        missing_fields=("unit",),
        facts=unknown_unit_facts,
    )
    assert item.unit_canonical is None

    with pytest.raises(
        ValidationError, match="normalized value does not match quantity"
    ):
        _rfq_item(facts=(_fact("quantity", "5001"), *_rfq_item().facts[1:]))


def test_rfq_plan_requires_provenance_for_top_level_fields() -> None:
    facts = tuple(
        _fact(name, value)
        for name, value in (
            ("requested_currency", "USD"),
            ("customer_id", None),
            ("named_place", None),
            ("requested_delivery_date", None),
        )
    )
    with pytest.raises(ValidationError, match="missing provenance fact for trade_term"):
        RFQPlan(
            rfq_revision_id="rfqr_demo_01",
            items=(_rfq_item(),),
            facts=facts,
        )


def test_rfq_revision_requires_parent_after_first_revision() -> None:
    with pytest.raises(
        ValidationError, match="only revision 1 may omit parent_revision_id"
    ):
        RFQRevision(
            org_id="org_demo_01",
            rfq_id="rfq_demo_01",
            revision_id="rfqr_demo_01",
            revision_no=2,
            plan=_plan(),
            created_at=CREATED_AT,
            created_by="sales_demo_01",
        )


def test_quote_snapshot_calculates_stable_customer_content_hash() -> None:
    quote = _quote_revision()
    same_content = quote.model_copy(
        update={
            "quotation_id": "quote_demo_02",
            "revision_id": "quote_rev_demo_02",
            "revision_no": 2,
            "created_by": "reviewer_demo_01",
        }
    )
    changed_content = quote.model_copy(
        update={"response_body": "Price valid for 30 days."}
    )

    assert quote.items[0].line_amount == Decimal("400.00")
    assert quote.content_hash == same_content.content_hash
    assert quote.content_hash != changed_content.content_hash
    assert len(quote.content_hash) == 64


def test_quote_hash_ignores_revision_specific_line_ids() -> None:
    quote = _quote_revision()
    first_line = quote.items[0].model_copy(
        update={"quote_item_id": "line-a", "rfq_item_id": "item-a"}
    )
    second_product = quote.items[0].product.model_copy(
        update={"sku": "BOLT-M8-40-A2", "name": "Hex bolt M8 x 40 A2-70"}
    )
    second_line = quote.items[0].model_copy(
        update={
            "quote_item_id": "line-b",
            "rfq_item_id": "item-b",
            "product": second_product,
        }
    )

    def with_items(
        items: tuple[QuoteLineSnapshot, ...], revision_id: str
    ) -> QuoteRevision:
        payload = quote.model_dump(exclude={"content_hash"})
        payload.update({
            "revision_id": revision_id,
            "revision_no": 2,
            "parent_revision_id": quote.revision_id,
            "items": items,
        })
        return QuoteRevision.model_validate(payload)

    first_revision = with_items(
        (
            first_line.model_copy(update={"quote_item_id": "a-line"}),
            second_line.model_copy(update={"quote_item_id": "b-line"}),
        ),
        "quote_rev_demo_02",
    )
    second_revision = with_items(
        (
            first_line.model_copy(update={"quote_item_id": "z-line"}),
            second_line.model_copy(update={"quote_item_id": "a-line"}),
        ),
        "quote_rev_demo_03",
    )

    assert first_revision.content_hash == second_revision.content_hash


def test_quote_snapshot_rejects_inconsistent_amount_or_versions() -> None:
    quote = _quote_revision()
    invalid_line = quote.items[0].model_copy(update={"line_amount": Decimal("40.00")})

    with pytest.raises(ValidationError, match="line_amount must match"):
        QuoteLineSnapshot.model_validate(invalid_line.model_dump())

    with pytest.raises(ValidationError, match="quote price-list version"):
        QuoteRevision.model_validate({
            **quote.model_dump(exclude={"content_hash"}),
            "price_list_version": "OTHER-PRICE-LIST",
        })


def test_quote_line_amount_validation_handles_large_decimal_quantities() -> None:
    line = (
        _quote_revision()
        .items[0]
        .model_copy(
            update={
                "quantity": Decimal("999999999999999999999999999999"),
                "unit_price": Decimal("0.08"),
                "line_amount": Decimal("79999999999999999999999999999.92"),
            }
        )
    )

    validated = QuoteLineSnapshot.model_validate(line.model_dump())

    assert validated.line_amount == Decimal("79999999999999999999999999999.92")
