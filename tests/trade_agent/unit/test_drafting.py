from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.contracts import (
    CanonicalUnit,
    FactOrigin,
    FieldFact,
    RFQItem,
    RFQPlan,
    RFQRevision,
    SpecificationFact,
)
from trade_agent.drafting import (
    DraftBlockerCode,
    ManualProductSelection,
    QuoteDraftRequest,
    build_quote_draft,
)

AS_OF = date(2026, 9, 29)
CREATED_AT = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


def _fact(field: str, value: str | None) -> FieldFact:
    return FieldFact(
        field_name=field,
        origin=FactOrigin.EXTRACTED,
        normalized_value=value,
    )


def _rfq_item(
    *,
    material: str | None = "stainless steel",
    quantity: Decimal | None = Decimal("5000"),
    unit: CanonicalUnit | None = CanonicalUnit.PIECE,
    additional_specs: tuple[SpecificationFact, ...] = (),
) -> RFQItem:
    specs = (
        SpecificationFact(name="material", value=material),
        SpecificationFact(name="diameter_mm", value="M8"),
        SpecificationFact(name="length_mm", value="30 mm"),
        *additional_specs,
    )
    facts = [
        _fact("quantity", format(quantity.normalize(), "f") if quantity else None),
        _fact("unit", unit.value if unit else None),
    ]
    facts.extend(_fact(f"specifications.{spec.name}", spec.value) for spec in specs)
    missing_fields = tuple(
        field
        for field, value in (("quantity", quantity), ("unit", unit))
        if value is None
    ) + tuple(spec.name for spec in specs if spec.value is None)
    return RFQItem(
        rfq_item_id="item_001",
        description_raw="stainless steel bolt M8 x 30",
        quantity=quantity,
        unit_raw=(
            "pcs"
            if unit is CanonicalUnit.PIECE
            else "boxes"
            if unit is CanonicalUnit.BOX
            else "cartons"
        ),
        unit_canonical=unit,
        specifications=specs,
        missing_fields=missing_fields,
        facts=tuple(facts),
    )


def _rfq_revision(item: RFQItem | None = None) -> RFQRevision:
    return RFQRevision(
        org_id="org_demo_01",
        rfq_id="rfq_demo_01",
        revision_id="rfqr_demo_01",
        revision_no=1,
        plan=RFQPlan(
            rfq_revision_id="rfqr_demo_01",
            items=(item or _rfq_item(),),
            facts=(
                _fact("requested_currency", "USD"),
                _fact("customer_id", None),
                _fact("trade_term", None),
                _fact("named_place", None),
                _fact("requested_delivery_date", None),
            ),
        ),
        created_at=CREATED_AT,
        created_by="sales_demo_01",
    )


def _request(
    *,
    item: RFQItem | None = None,
    selections: tuple[ManualProductSelection, ...] = (
        ManualProductSelection("item_001", "BOLT-M8-30-A2"),
    ),
    valid_until: date | None = date(2026, 10, 29),
    catalog=None,
) -> QuoteDraftRequest:
    return QuoteDraftRequest(
        rfq_revision=_rfq_revision(item),
        catalog=catalog or build_synthetic_catalog(),
        selected_products=selections,
        quotation_id="quote_demo_01",
        revision_id="quote_rev_demo_01",
        revision_no=1,
        parent_revision_id=None,
        created_by="sales_demo_01",
        created_at=CREATED_AT,
        as_of=AS_OF,
        valid_until=valid_until,
        template_version="quote-v1",
    )


def test_confirmed_sku_builds_a_sourced_quote_snapshot() -> None:
    result = build_quote_draft(_request())

    assert result.is_ready
    assert result.blockers == ()
    assert result.quote_revision is not None
    assert result.quote_revision.items[0].product.sku == "BOLT-M8-30-A2"
    assert result.quote_revision.items[0].product.catalog_version == "SYNTH-CAT-1"
    assert result.quote_revision.price_list_version == "SYNTH-USD-2026-01"
    assert result.quote_revision.items[0].price_list_source.startswith("Synthetic")
    assert result.quote_revision.items[0].unit_price == Decimal("0.080")
    assert result.quote_revision.items[0].line_amount == Decimal("400.00")
    assert len(result.quote_revision.content_hash) == 64


def test_unconfirmed_selection_returns_candidates_without_a_partial_quote() -> None:
    result = build_quote_draft(_request(selections=()))

    assert result.quote_revision is None
    assert result.item_reviews[0].selected_match is None
    assert result.item_reviews[0].search_result.candidates
    assert result.blockers[0].code is DraftBlockerCode.SELECTION_REQUIRED


def test_manual_sku_choice_cannot_override_hard_specification_conflict() -> None:
    item = _rfq_item(material="carbon steel")

    result = build_quote_draft(_request(item=item))

    assert result.quote_revision is None
    assert result.item_reviews[0].selected_match is not None
    assert result.blockers[0].code is DraftBlockerCode.SPECIFICATION_CONFLICT
    assert "material" in result.blockers[0].details


def test_unknown_required_certification_blocks_quote_draft() -> None:
    item = _rfq_item(
        additional_specs=(SpecificationFact(name="certification", value="ISO 9001"),)
    )

    result = build_quote_draft(_request(item=item))

    assert result.quote_revision is None
    assert any(
        blocker.code is DraftBlockerCode.SPECIFICATION_UNKNOWN
        and "certification" in blocker.details
        for blocker in result.blockers
    )


def test_missing_quantity_unit_price_or_validity_prevents_quote_creation() -> None:
    no_price_catalog = build_synthetic_catalog()
    no_price_catalog = replace(
        no_price_catalog,
        prices=tuple(
            price for price in no_price_catalog.prices if price.sku != "BOLT-M8-30-A2"
        ),
    )
    missing_quantity = build_quote_draft(_request(item=_rfq_item(quantity=None)))
    missing_unit = build_quote_draft(_request(item=_rfq_item(unit=None)))
    missing_price = build_quote_draft(_request(catalog=no_price_catalog))
    missing_validity = build_quote_draft(_request(valid_until=None))
    past_validity = build_quote_draft(_request(valid_until=date(2026, 9, 28)))

    assert missing_quantity.quote_revision is None
    assert any(
        blocker.code is DraftBlockerCode.RFQ_ITEM_INCOMPLETE
        and "quantity" in blocker.details
        for blocker in missing_quantity.blockers
    )
    assert missing_unit.quote_revision is None
    assert any(
        blocker.code is DraftBlockerCode.RFQ_ITEM_INCOMPLETE
        and "unit" in blocker.details
        for blocker in missing_unit.blockers
    )
    assert missing_price.quote_revision is None
    assert any(
        blocker.code is DraftBlockerCode.PRICE_UNAVAILABLE
        for blocker in missing_price.blockers
    )
    assert missing_validity.quote_revision is None
    assert missing_validity.blockers[0].code is DraftBlockerCode.VALIDITY_REQUIRED
    assert past_validity.quote_revision is None
    assert past_validity.blockers[0].code is DraftBlockerCode.VALIDITY_IN_PAST


def test_box_quantity_without_pack_size_is_not_converted_to_pieces() -> None:
    result = build_quote_draft(
        _request(item=_rfq_item(quantity=Decimal("5"), unit=CanonicalUnit.BOX))
    )

    assert result.quote_revision is None
    assert result.pricing_results[0].quantity == Decimal("5")
    assert result.pricing_results[0].unit == CanonicalUnit.BOX.value
    assert result.pricing_results[0].line_amount is None
    assert result.blockers[0].code is DraftBlockerCode.PRICE_UNAVAILABLE
    assert "UNIT_MISMATCH" in result.blockers[0].details
