"""Run an offline M1 quote-draft demonstration using synthetic fastener data."""

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import sys

from trade_agent.catalog import build_synthetic_catalog
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
from trade_agent.drafting import (
    DraftBlocker,
    ManualProductSelection,
    QuoteDraftRequest,
    build_quote_draft,
)


def main() -> int:
    catalog = build_synthetic_catalog()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sku",
        required=True,
        choices=tuple(sorted(product.sku for product in catalog.products)),
        help="SKU explicitly confirmed by the salesperson",
    )
    parser.add_argument(
        "--quantity",
        default="5000",
        help="synthetic demo quantity, in pieces (default: 5000)",
    )
    args = parser.parse_args()

    try:
        quantity = Decimal(args.quantity)
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be finite and greater than zero")
    except (InvalidOperation, ValueError) as exc:
        parser.error(str(exc))

    item = _demo_item(quantity)
    rfq_revision = _demo_rfq_revision(item)
    as_of = date.today()
    result = build_quote_draft(
        QuoteDraftRequest(
            rfq_revision=rfq_revision,
            catalog=catalog,
            selected_products=(ManualProductSelection(item.rfq_item_id, args.sku),),
            quotation_id="quote_demo_m1",
            revision_id="quote_revision_demo_m1",
            revision_no=1,
            parent_revision_id=None,
            created_by="sales_demo_01",
            created_at=datetime.now(UTC),
            as_of=as_of,
            valid_until=as_of + timedelta(days=30),
            template_version="quote-v1",
            response_body="Please review this synthetic demonstration draft.",
        )
    )
    if not result.is_ready or result.quote_revision is None:
        sys.stdout.write(
            f"{
                json.dumps(
                    {
                        'synthetic_data': True,
                        'quote_ready': False,
                        'blockers': [
                            _blocker_payload(blocker) for blocker in result.blockers
                        ],
                    },
                    indent=2,
                )
            }\n"
        )
        return 1

    revision = result.quote_revision
    sys.stdout.write(
        f"{
            json.dumps(
                {
                    'synthetic_data': True,
                    'quote_ready': True,
                    'rfq_revision_id': revision.rfq_revision_id,
                    'quotation_id': revision.quotation_id,
                    'revision_id': revision.revision_id,
                    'content_hash': revision.content_hash,
                    'valid_until': revision.valid_until.isoformat(),
                    'catalog_version': revision.catalog_version,
                    'price_list_version': revision.price_list_version,
                    'lines': [
                        {
                            'rfq_item_id': line.rfq_item_id,
                            'sku': line.product.sku,
                            'product_name': line.product.name,
                            'quantity': format(line.quantity, 'f'),
                            'unit': line.unit.value,
                            'currency': line.currency.value,
                            'unit_price': format(line.unit_price, 'f'),
                            'line_amount': format(line.line_amount, 'f'),
                            'price_list_source': line.price_list_source,
                        }
                        for line in revision.items
                    ],
                },
                indent=2,
            )
        }\n"
    )
    return 0


def _demo_item(quantity: Decimal) -> RFQItem:
    evidence = SourceEvidence(
        field="quantity",
        source_document_id="doc_synthetic_demo_01",
        location="body:line:2",
        quote=f"{quantity:f} pcs stainless steel hex bolts M8 x 30 A2-70",
    )
    specifications = (
        SpecificationFact(name="material", value="stainless steel"),
        SpecificationFact(name="grade", value="A2-70"),
        SpecificationFact(name="diameter_mm", value="M8"),
        SpecificationFact(name="length_mm", value="30 mm"),
        SpecificationFact(name="standard", value="ISO 4014"),
    )
    facts = (
        FieldFact(
            field_name="quantity",
            origin=FactOrigin.EXTRACTED,
            raw_value=f"{quantity:f} pcs",
            normalized_value=format(quantity.normalize(), "f"),
            evidence=(evidence,),
        ),
        FieldFact(
            field_name="unit",
            origin=FactOrigin.EXTRACTED,
            raw_value="pcs",
            normalized_value=CanonicalUnit.PIECE.value,
            evidence=(evidence,),
        ),
        *(
            FieldFact(
                field_name=f"specifications.{specification.name}",
                origin=FactOrigin.EXTRACTED,
                raw_value=specification.value,
                normalized_value=specification.value,
                evidence=(evidence,),
            )
            for specification in specifications
        ),
    )
    return RFQItem(
        rfq_item_id="item_demo_m1_01",
        description_raw=evidence.quote,
        quantity=quantity,
        unit_raw="pcs",
        unit_canonical=CanonicalUnit.PIECE,
        specifications=specifications,
        facts=facts,
        evidence=(evidence,),
    )


def _demo_rfq_revision(item: RFQItem) -> RFQRevision:
    top_level_facts = (
        ("requested_currency", "USD"),
        ("customer_id", None),
        ("trade_term", None),
        ("named_place", None),
        ("requested_delivery_date", None),
    )
    plan = RFQPlan(
        rfq_revision_id="rfq_revision_demo_m1",
        items=(item,),
        facts=tuple(
            FieldFact(
                field_name=field,
                origin=FactOrigin.POLICY,
                normalized_value=value,
            )
            for field, value in top_level_facts
        ),
    )
    return RFQRevision(
        org_id="org_synthetic_demo_01",
        rfq_id="rfq_synthetic_demo_01",
        revision_id=plan.rfq_revision_id,
        revision_no=1,
        plan=plan,
        created_at=datetime.now(UTC),
        created_by="sales_demo_01",
    )


def _blocker_payload(blocker: DraftBlocker) -> dict[str, object]:
    return {
        "code": blocker.code.value,
        "rfq_item_id": blocker.rfq_item_id,
        "details": blocker.details,
    }


if __name__ == "__main__":
    raise SystemExit(main())
