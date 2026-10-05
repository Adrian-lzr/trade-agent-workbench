"""Immutable, validated contracts for RFQ and quote business snapshots."""

from __future__ import annotations

# Pydantic resolves these annotations at runtime to build request schemas.
from datetime import UTC, date, datetime  # noqa: TC003
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
import json
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

_LANGUAGE_TAG_MIN_LENGTH = 2
_LANGUAGE_TAG_MAX_LENGTH = 16


class ContractModel(BaseModel):
    """Base for immutable public contracts with no silently ignored fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class CanonicalUnit(StrEnum):
    PIECE = "piece"
    BOX = "box"
    CARTON = "carton"
    KILOGRAM = "kg"
    SET = "set"


class Currency(StrEnum):
    USD = "USD"


class FactOrigin(StrEnum):
    EXTRACTED = "extracted"
    USER_CONFIRMED = "user_confirmed"
    CATALOG = "catalog"
    POLICY = "policy"


class AggregateType(StrEnum):
    RFQ = "rfq"
    QUOTATION = "quotation"


class SourceEvidence(ContractModel):
    field: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    source_document_id: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=1, max_length=2000)


class FieldFact(ContractModel):
    """Provenance for one normalized business fact."""

    field_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    origin: FactOrigin
    raw_value: str | None = Field(default=None, max_length=2000)
    normalized_value: str | None = Field(default=None, max_length=2000)
    confirmed_at: datetime | None = None
    confirmed_by: str | None = Field(default=None, max_length=128)
    evidence: tuple[SourceEvidence, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def require_confirmation_actor(self) -> FieldFact:
        if self.origin == FactOrigin.USER_CONFIRMED and not (
            self.confirmed_at and self.confirmed_by
        ):
            raise ValueError(
                "user-confirmed facts require confirmed_at and confirmed_by"
            )
        if self.confirmed_at and self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmed_at must include a timezone")
        return self


class SpecificationFact(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    value: str | None = Field(default=None, max_length=256)


class RFQItem(ContractModel):
    rfq_item_id: str = Field(min_length=1, max_length=128)
    description_raw: str = Field(min_length=1, max_length=4000)
    quantity: Decimal | None = None
    unit_raw: str | None = Field(default=None, max_length=64)
    unit_canonical: CanonicalUnit | None = None
    specifications: tuple[SpecificationFact, ...] = Field(default=(), max_length=50)
    missing_fields: tuple[str, ...] = Field(default=(), max_length=50)
    facts: tuple[FieldFact, ...] = Field(default=(), max_length=100)
    evidence: tuple[SourceEvidence, ...] = Field(default=(), max_length=100)

    @field_validator("quantity")
    @classmethod
    def require_positive_finite_quantity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("quantity must be a finite value greater than zero")
        return value

    @model_validator(mode="after")
    def validate_missing_fields(self) -> RFQItem:
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise ValueError("missing_fields must not contain duplicates")
        missing = set(self.missing_fields)
        if (self.quantity is None) != ("quantity" in missing):
            raise ValueError("missing_fields must accurately reflect quantity")
        if (self.unit_canonical is None) != ("unit" in missing):
            raise ValueError("missing_fields must accurately reflect unit")
        missing_specs = {
            spec.name for spec in self.specifications if spec.value is None
        }
        known_specs = {
            spec.name for spec in self.specifications if spec.value is not None
        }
        if not missing_specs.issubset(missing):
            raise ValueError("missing_fields must include every unknown specification")
        if known_specs.intersection(missing):
            raise ValueError("known specifications cannot appear in missing_fields")
        return self

    @model_validator(mode="after")
    def validate_unique_specs_and_provenance(self) -> RFQItem:
        names = [spec.name for spec in self.specifications]
        if len(set(names)) != len(names):
            raise ValueError("specification names must be unique per item")
        fact_names = [fact.field_name for fact in self.facts]
        if len(set(fact_names)) != len(fact_names):
            raise ValueError("fact names must be unique per item")
        expected_facts = {
            "quantity": (
                format(self.quantity.normalize(), "f")
                if self.quantity is not None
                else None
            ),
            "unit": self.unit_canonical.value
            if self.unit_canonical is not None
            else None,
        }
        expected_facts.update({
            f"specifications.{spec.name}": spec.value for spec in self.specifications
        })
        facts_by_name = {fact.field_name: fact for fact in self.facts}
        for name, normalized_value in expected_facts.items():
            fact = facts_by_name.get(name)
            if fact is None:
                raise ValueError(f"missing provenance fact for {name}")
            if fact.normalized_value != normalized_value:
                raise ValueError(f"normalized value does not match {name}")
        return self


class RFQPlan(ContractModel):
    schema_version: Literal[1] = 1
    rfq_revision_id: str = Field(min_length=1, max_length=128)
    customer_id: str | None = Field(default=None, max_length=128)
    requested_currency: Currency = Currency.USD
    trade_term: str | None = Field(default=None, max_length=32)
    named_place: str | None = Field(default=None, max_length=128)
    requested_delivery_date: date | None = None
    items: tuple[RFQItem, ...] = Field(min_length=1, max_length=50)
    facts: tuple[FieldFact, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def require_unique_item_ids(self) -> RFQPlan:
        item_ids = [item.rfq_item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("rfq_item_id values must be unique within a revision")
        fact_names = [fact.field_name for fact in self.facts]
        if len(set(fact_names)) != len(fact_names):
            raise ValueError("fact names must be unique within an RFQ revision")
        expected_facts = {
            "requested_currency": self.requested_currency.value,
            "customer_id": self.customer_id,
            "trade_term": self.trade_term,
            "named_place": self.named_place,
            "requested_delivery_date": (
                self.requested_delivery_date.isoformat()
                if self.requested_delivery_date is not None
                else None
            ),
        }
        facts_by_name = {fact.field_name: fact for fact in self.facts}
        for name, normalized_value in expected_facts.items():
            fact = facts_by_name.get(name)
            if fact is None:
                raise ValueError(f"missing provenance fact for {name}")
            if fact.normalized_value != normalized_value:
                raise ValueError(f"normalized value does not match {name}")
        return self


class RFQRevision(ContractModel):
    org_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_no: int = Field(ge=1)
    parent_revision_id: str | None = Field(default=None, max_length=128)
    plan: RFQPlan
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_revision_identity(self) -> RFQRevision:
        if self.plan.rfq_revision_id != self.revision_id:
            raise ValueError("plan revision ID must match revision_id")
        if (self.revision_no == 1) != (self.parent_revision_id is None):
            raise ValueError("only revision 1 may omit parent_revision_id")
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return self


class ProductSnapshot(ContractModel):
    sku: str = Field(min_length=1, max_length=80)
    catalog_version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    specifications: tuple[SpecificationFact, ...] = Field(default=(), max_length=50)


class QuoteLineSnapshot(ContractModel):
    quote_item_id: str = Field(min_length=1, max_length=128)
    rfq_item_id: str = Field(min_length=1, max_length=128)
    product: ProductSnapshot
    quantity: Decimal
    unit: CanonicalUnit
    price_list_version: str = Field(min_length=1, max_length=64)
    price_list_source: str = Field(min_length=1, max_length=255)
    unit_price: Decimal
    price_unit: CanonicalUnit
    currency: Currency = Currency.USD
    line_amount: Decimal

    @field_validator("quantity")
    @classmethod
    def require_positive_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("quantity must be a finite value greater than zero")
        return value

    @field_validator("unit_price")
    @classmethod
    def require_positive_unit_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("unit_price must be a finite value greater than zero")
        return value

    @field_validator("line_amount")
    @classmethod
    def require_finite_line_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("line_amount must be a finite non-negative value")
        return value

    @model_validator(mode="after")
    def validate_price_snapshot(self) -> QuoteLineSnapshot:
        if self.unit != self.price_unit:
            raise ValueError("quote unit must match the price-list unit")
        precision = max(
            28,
            len(self.quantity.as_tuple().digits)
            + len(self.unit_price.as_tuple().digits)
            + 2,
            max(1, self.quantity.adjusted() + self.unit_price.adjusted() + 3) + 2,
        )
        with localcontext() as context:
            context.prec = precision
            expected = (self.unit_price * self.quantity).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if self.line_amount != expected:
            raise ValueError(
                "line_amount must match the rounded quantity and unit price"
            )
        return self


class QuoteRevision(ContractModel):
    org_id: str = Field(min_length=1, max_length=128)
    quotation_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_no: int = Field(ge=1)
    parent_revision_id: str | None = Field(default=None, max_length=128)
    rfq_revision_id: str = Field(min_length=1, max_length=128)
    catalog_version: str = Field(min_length=1, max_length=64)
    price_list_version: str = Field(min_length=1, max_length=64)
    currency: Currency = Currency.USD
    items: tuple[QuoteLineSnapshot, ...] = Field(min_length=1, max_length=50)
    trade_term: str | None = Field(default=None, max_length=32)
    named_place: str | None = Field(default=None, max_length=128)
    delivery_date: date | None = None
    valid_until: date | None = None
    response_body: str | None = Field(default=None, max_length=20000)
    template_version: str = Field(min_length=1, max_length=64)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_quote_snapshot(self) -> QuoteRevision:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if (self.revision_no == 1) != (self.parent_revision_id is None):
            raise ValueError("only revision 1 may omit parent_revision_id")
        item_ids = [item.quote_item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("quote_item_id values must be unique within a revision")
        for item in self.items:
            if item.product.catalog_version != self.catalog_version:
                raise ValueError(
                    "all product snapshots must use the quote catalog version"
                )
            if item.price_list_version != self.price_list_version:
                raise ValueError(
                    "all quote lines must use the quote price-list version"
                )
            if item.currency != self.currency:
                raise ValueError("all quote lines must use the quote currency")
        return self

    @computed_field
    @property
    def content_hash(self) -> str:
        """Hash only customer-visible content and its rendering template version."""
        item_payloads = [
            {
                "currency": item.currency.value,
                "line_amount": format(item.line_amount, "f"),
                "product_name": item.product.name,
                "product_specifications": [
                    {"name": spec.name, "value": spec.value}
                    for spec in sorted(
                        item.product.specifications, key=lambda spec: spec.name
                    )
                ],
                "quantity": format(item.quantity.normalize(), "f"),
                "sku": item.product.sku,
                "unit": item.unit.value,
                "unit_price": format(item.unit_price, "f"),
            }
            for item in self.items
        ]
        payload = {
            "currency": self.currency.value,
            "delivery_date": self.delivery_date.isoformat()
            if self.delivery_date
            else None,
            "items": item_payloads,
            "named_place": self.named_place,
            "response_body": self.response_body,
            "template_version": self.template_version,
            "trade_term": self.trade_term,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


class ExportDocumentType(StrEnum):
    """Shipment-facing documents covered by the consistency gate."""

    COMMERCIAL_INVOICE = "commercial_invoice"
    PACKING_LIST = "packing_list"


class CommercialInvoiceLine(ContractModel):
    """Customer-visible invoice line copied from one approved quote line."""

    quote_item_id: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=4000)
    quantity: Decimal
    uom: CanonicalUnit
    unit_price: Decimal
    amount: Decimal

    @field_validator("quantity")
    @classmethod
    def require_positive_invoice_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("quantity must be a finite value greater than zero")
        return value

    @field_validator("unit_price")
    @classmethod
    def require_nonnegative_unit_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("unit_price must be a finite non-negative value")
        return value

    @field_validator("amount")
    @classmethod
    def require_nonnegative_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("amount must be a finite non-negative value")
        return value


class PackingListPackage(ContractModel):
    """One package row with explicit quantity and consistent weight units."""

    package_id: str = Field(min_length=1, max_length=128)
    quote_item_id: str = Field(min_length=1, max_length=128)
    marks: str = Field(min_length=1, max_length=1000)
    quantity: Decimal
    uom: CanonicalUnit
    net_weight: Decimal
    gross_weight: Decimal
    weight_uom: str = Field(min_length=1, max_length=16)

    @field_validator("quantity", "net_weight", "gross_weight")
    @classmethod
    def require_nonnegative_measurement(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("measurements must be finite and non-negative")
        return value

    @field_validator("quantity")
    @classmethod
    def require_positive_package_quantity(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("package quantity must be greater than zero")
        return value

    @model_validator(mode="after")
    def validate_weights(self) -> PackingListPackage:
        if self.gross_weight < self.net_weight:
            raise ValueError("gross_weight must be at least net_weight")
        return self


class CommercialInvoiceDocument(ContractModel):
    """Immutable input snapshot for a commercial invoice revision."""

    buyer: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("buyer", "buyer_name"),
    )
    consignee: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("consignee", "consignee_name"),
    )
    document_reference: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("document_reference", "reference"),
    )
    currency: Currency = Currency.USD
    incoterm: str = Field(min_length=1, max_length=32)
    named_place: str | None = Field(default=None, max_length=128)
    lines: tuple[CommercialInvoiceLine, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_unique_line_ids(self) -> CommercialInvoiceDocument:
        ids = [line.quote_item_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("quote_item_id values must be unique in an invoice")
        return self


class PackingListDocument(ContractModel):
    """Immutable input snapshot for a packing-list revision."""

    buyer: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("buyer", "buyer_name"),
    )
    consignee: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("consignee", "consignee_name"),
    )
    document_reference: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("document_reference", "reference"),
    )
    incoterm: str = Field(min_length=1, max_length=32)
    named_place: str | None = Field(default=None, max_length=128)
    packages: tuple[PackingListPackage, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_unique_package_ids(self) -> PackingListDocument:
        ids = [package.package_id for package in self.packages]
        if len(ids) != len(set(ids)):
            raise ValueError("package_id values must be unique in a packing list")
        return self


class ShipmentHandoffStatus(StrEnum):
    """Operational shipment handoff states."""

    DRAFT = "draft"
    READY_FOR_BOOKING = "ready_for_booking"
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ShipmentEvidenceType(StrEnum):
    """Evidence/checklist categories used by the shipment gate."""

    FREIGHT_QUOTE = "freight_quote"
    INSURANCE = "insurance"
    PACKING_CHECK = "packing_check"
    LABEL_CHECK = "label_check"
    REQUIRED_DOCUMENT = "required_document"
    BOOKING = "booking"
    BILL_OF_LADING = "bill_of_lading"
    AIR_WAYBILL = "air_waybill"
    EXPORT_DOCUMENT = "export_document"
    IMPORT_DOCUMENT = "import_document"
    OTHER = "other"


class ShipmentEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    REJECTED = "rejected"
    VOID = "void"


class ShipmentGateDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PaymentMethod(StrEnum):
    """Common international payment methods; labels do not score buyer credit."""

    CASH_IN_ADVANCE = "cash_in_advance"
    LETTER_OF_CREDIT = "letter_of_credit"
    DOCUMENTARY_COLLECTION = "documentary_collection"
    OPEN_ACCOUNT = "open_account"
    CONSIGNMENT = "consignment"
    OTHER = "other"


class PaymentEvidenceType(StrEnum):
    INSURANCE = "insurance"
    GUARANTEE = "guarantee"
    OTHER = "other"


class PaymentEvidenceStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    VOID = "void"


class PaymentTaskKind(StrEnum):
    DEPOSIT = "deposit"
    BALANCE = "balance"
    OTHER = "other"


class PaymentTaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PaymentTermsPlan(ContractModel):
    """Reviewer-confirmed payment instructions attached to one quote snapshot."""

    org_id: str = Field(min_length=1, max_length=128)
    quotation_id: str = Field(min_length=1, max_length=128)
    quote_revision_id: str = Field(min_length=1, max_length=128)
    quote_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    payment_method: PaymentMethod
    terms_summary: str = Field(min_length=1, max_length=2000)
    deposit_percent: Decimal | None = Field(default=None, ge=0, le=100)
    balance_condition: str = Field(min_length=1, max_length=1000)
    requires_insurance_or_guarantee: bool = False

    @field_validator("deposit_percent")
    @classmethod
    def finite_deposit_percent(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("deposit_percent must be finite")
        return value


class PaymentRiskEvidence(ContractModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_type: PaymentEvidenceType
    evidence_ref: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: PaymentEvidenceStatus = PaymentEvidenceStatus.PENDING
    expires_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("expires_at")
    @classmethod
    def normalize_payment_evidence_expiry(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("evidence expiry must include a timezone")
            return value.astimezone(UTC)
        return value


class PaymentDueTask(ContractModel):
    task_id: str = Field(min_length=1, max_length=128)
    kind: PaymentTaskKind
    due_at: datetime
    amount: Decimal = Field(gt=0)
    condition: str = Field(min_length=1, max_length=1000)
    blocks_shipment: bool = True

    @field_validator("due_at")
    @classmethod
    def normalize_payment_due_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("payment due_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("amount")
    @classmethod
    def finite_payment_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("payment amount must be finite and greater than zero")
        return value


class ShipmentHandoff(ContractModel):
    """Mutable aggregate fields for a handoff, anchored to an approved quote."""

    shipment_handoff_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    quotation_id: str = Field(min_length=1, max_length=128)
    quote_revision_id: str = Field(min_length=1, max_length=128)
    approved_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_incoterm: str = Field(min_length=1, max_length=32)
    named_place: str = Field(min_length=1, max_length=128)
    responsibility_split: dict[str, str] = Field(default_factory=dict, max_length=32)
    freight_forwarder_name: str | None = Field(default=None, max_length=255)
    freight_forwarder_quote_ref: str | None = Field(default=None, max_length=512)
    carrier_name: str | None = Field(default=None, max_length=255)
    insurance_scope: str | None = Field(default=None, max_length=2000)
    insurance_expires_at: datetime | None = None
    document_set_id: str | None = Field(default=None, max_length=128)
    required_documents: tuple[str, ...] = Field(default=(), max_length=100)
    booking_reference: str | None = Field(default=None, max_length=255)
    eta: datetime | None = None

    @field_validator("insurance_expires_at", "eta")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shipment timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_required_document_keys(self) -> ShipmentHandoff:
        normalized = tuple(item.strip() for item in self.required_documents)
        if any(not item for item in normalized):
            raise ValueError("required document keys must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("required document keys must be unique")
        return self


class ShipmentEvidence(ContractModel):
    """One immutable shipment evidence/checklist revision."""

    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_type: ShipmentEvidenceType
    check_key: str | None = Field(default=None, max_length=128)
    evidence_ref: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ShipmentEvidenceStatus = ShipmentEvidenceStatus.VERIFIED
    notes: str | None = Field(default=None, max_length=4000)
    expires_at: datetime | None = None
    parent_evidence_id: str | None = Field(default=None, max_length=128)

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence expiry must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_check_key_for_checklists(self) -> ShipmentEvidence:
        if (
            self.evidence_type
            in {
                ShipmentEvidenceType.REQUIRED_DOCUMENT,
                ShipmentEvidenceType.EXPORT_DOCUMENT,
                ShipmentEvidenceType.IMPORT_DOCUMENT,
            }
            and not self.check_key
        ):
            raise ValueError("document evidence requires check_key")
        return self


class AuditEvent(ContractModel):
    event_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    aggregate_type: AggregateType
    aggregate_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    prior_revision_id: str | None = Field(default=None, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime

    @model_validator(mode="after")
    def require_timezone(self) -> AuditEvent:
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return self


class InquiryCustomerRole(StrEnum):
    """Business role of the party sending an overseas inquiry."""

    END_CUSTOMER = "end_customer"
    REPRESENTATIVE = "representative"
    DISTRIBUTOR = "distributor"
    UNKNOWN = "unknown"


class InquiryQueueState(StrEnum):
    """Operational state for the response queue."""

    OPEN = "open"
    OVERDUE = "overdue"
    RESPONDED = "responded"
    NURTURE = "nurture"
    CLOSED = "closed"


class TranslationReviewState(StrEnum):
    """State of human review for a translated customer-facing reply."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TranslationReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class InquiryAttachment(ContractModel):
    """A controlled reference to an inquiry or reply attachment.

    Binary content stays outside the business database. The immutable reference
    and digest let reviewers verify which customer-visible file was attached.
    """

    attachment_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=128)
    storage_ref: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^private://[^\s]+$",
    )
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


def _validate_attachment_ids(
    attachments: tuple[InquiryAttachment, ...],
) -> None:
    ids = [attachment.attachment_id for attachment in attachments]
    if len(ids) != len(set(ids)):
        raise ValueError("attachment_id values must be unique within a record")


class InquiryCase(ContractModel):
    """Immutable identity and SLA facts for one inbound inquiry."""

    org_id: str = Field(min_length=1, max_length=128)
    inquiry_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    source_channel: str = Field(min_length=1, max_length=64)
    customer_role: InquiryCustomerRole = InquiryCustomerRole.UNKNOWN
    original_language: str = Field(min_length=2, max_length=16)
    received_at: datetime
    response_due_at: datetime
    owner_id: str | None = Field(default=None, max_length=128)
    queue_state: InquiryQueueState = InquiryQueueState.OPEN
    attachments: tuple[InquiryAttachment, ...] = Field(default=(), max_length=20)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @field_validator("original_language", mode="before")
    @classmethod
    def normalize_original_language(cls, value: object) -> str:
        return _normalize_language_tag(value)

    @model_validator(mode="after")
    def validate_sla_timestamps(self) -> InquiryCase:
        timestamps = (
            self.received_at,
            self.response_due_at,
            self.created_at,
        )
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("inquiry timestamps must include a timezone")
        if self.response_due_at < self.received_at:
            raise ValueError("response_due_at must not precede received_at")
        _validate_attachment_ids(self.attachments)
        return self


class InquiryReplyRevision(ContractModel):
    """Append-only customer reply snapshot tied to an RFQ revision."""

    org_id: str = Field(min_length=1, max_length=128)
    inquiry_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    reply_revision_id: str = Field(min_length=1, max_length=128)
    revision_no: int = Field(ge=1)
    parent_reply_revision_id: str | None = Field(default=None, max_length=128)
    rfq_revision_id: str = Field(min_length=1, max_length=128)
    source_language: str = Field(min_length=2, max_length=16)
    target_language: str = Field(min_length=2, max_length=16)
    source_content: str = Field(min_length=1, max_length=20000)
    translated_content: str | None = Field(default=None, max_length=20000)
    template_version: str = Field(default="reply-v1", min_length=1, max_length=64)
    attachments: tuple[InquiryAttachment, ...] = Field(default=(), max_length=20)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @field_validator("source_language", "target_language", mode="before")
    @classmethod
    def normalize_languages(cls, value: object) -> str:
        return _normalize_language_tag(value)

    @model_validator(mode="after")
    def validate_revision(self) -> InquiryReplyRevision:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if (self.revision_no == 1) != (self.parent_reply_revision_id is None):
            raise ValueError("only reply revision 1 may omit a parent revision")
        if self.requires_translation and not self.translated_content:
            raise ValueError(
                "translated_content is required when source and target languages differ"
            )
        _validate_attachment_ids(self.attachments)
        return self

    @computed_field
    @property
    def requires_translation(self) -> bool:
        return self.source_language.lower() != self.target_language.lower()

    @computed_field
    @property
    def content_hash(self) -> str:
        """Hash all customer-visible reply content and its rendering version."""
        payload = {
            "attachments": [
                attachment.model_dump(mode="json") for attachment in self.attachments
            ],
            "source_content": self.source_content,
            "source_language": self.source_language.lower(),
            "target_language": self.target_language.lower(),
            "template_version": self.template_version,
            "translated_content": self.translated_content,
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_language_tag(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("language tag must be a string")
    normalized = value.strip().replace("_", "-").lower()
    if (
        len(normalized) < _LANGUAGE_TAG_MIN_LENGTH
        or len(normalized) > _LANGUAGE_TAG_MAX_LENGTH
    ):
        raise ValueError("language tag length must be between 2 and 16")
    parts = normalized.split("-")
    if any(not part or not part.isalnum() for part in parts) or not parts[0].isalpha():
        raise ValueError("language tag must use alphanumeric BCP 47 subtags")
    return normalized


class QualificationResult(StrEnum):
    """Outcome of a buyer or inquiry party screening check."""

    CLEAR = "clear"
    POTENTIAL_MATCH = "potential_match"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class QualificationCheck(ContractModel):
    """Immutable evidence snapshot for one customer or inquiry target."""

    org_id: str = Field(min_length=1, max_length=128)
    check_id: str = Field(min_length=1, max_length=128)
    customer_id: str | None = Field(default=None, max_length=128)
    inquiry_id: str | None = Field(default=None, max_length=128)
    source: str = Field(min_length=1, max_length=255)
    reference: str = Field(min_length=1, max_length=1024)
    checked_at: datetime
    result: QualificationResult
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_attachment_ref: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=4000)
    reviewer_id: str | None = Field(default=None, max_length=128)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @field_validator("checked_at", "created_at")
    @classmethod
    def normalize_qualification_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("qualification timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_target_and_timestamp(self) -> QualificationCheck:
        if self.customer_id is None and self.inquiry_id is None:
            raise ValueError("qualification check must target a customer or inquiry")
        return self


class QualificationDecision(ContractModel):
    """Immutable authorized decision attached to a qualification check."""

    org_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    check_id: str = Field(min_length=1, max_length=128)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: QualificationResult
    reviewer_id: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def normalize_decision_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must include a timezone")
        return value.astimezone(UTC)
