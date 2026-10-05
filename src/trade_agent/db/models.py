"""SQLAlchemy schema for versioned product catalogs and price lists."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "trade_agent"


class Base(DeclarativeBase):
    """Base metadata for the trade-agent persistence schema."""


class CatalogVersion(Base):
    __tablename__ = "catalog_versions"
    __table_args__ = ({"schema": SCHEMA},)

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("diameter_mm > 0", name="ck_products_diameter_positive"),
        CheckConstraint("length_mm > 0", name="ck_products_length_positive"),
        {"schema": SCHEMA},
    )

    catalog_version: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.catalog_versions.version", ondelete="CASCADE"),
        primary_key=True,
    )
    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    standard: Mapped[str] = mapped_column(String(64), nullable=False)
    material: Mapped[str] = mapped_column(String(80), nullable=False)
    grade: Mapped[str] = mapped_column(String(32), nullable=False)
    diameter_mm: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    length_mm: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PriceList(Base):
    __tablename__ = "price_lists"
    __table_args__ = (
        UniqueConstraint(
            "version", "catalog_version", name="uq_price_lists_version_catalog"
        ),
        CheckConstraint("currency = 'USD'", name="ck_price_lists_currency_usd"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_price_lists_effective_range",
        ),
        {"schema": SCHEMA},
    )

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.catalog_versions.version", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PriceListItem(Base):
    __tablename__ = "price_list_items"
    __table_args__ = (
        CheckConstraint(
            "minimum_quantity > 0", name="ck_price_items_quantity_positive"
        ),
        CheckConstraint("unit_price > 0", name="ck_price_items_price_positive"),
        CheckConstraint("currency = 'USD'", name="ck_price_items_currency_usd"),
        ForeignKeyConstraint(
            ["price_list_version", "catalog_version"],
            [f"{SCHEMA}.price_lists.version", f"{SCHEMA}.price_lists.catalog_version"],
            ondelete="CASCADE",
            name="fk_price_items_list_catalog",
        ),
        ForeignKeyConstraint(
            ["catalog_version", "sku"],
            [f"{SCHEMA}.products.catalog_version", f"{SCHEMA}.products.sku"],
            ondelete="RESTRICT",
            name="fk_price_items_product",
        ),
        {"schema": SCHEMA},
    )

    price_list_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    minimum_quantity: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)


class RFQAggregate(Base):
    __tablename__ = "rfqs"
    __table_args__ = (
        UniqueConstraint("org_id", "rfq_id", name="uq_rfqs_org_id"),
        CheckConstraint(
            "(current_revision_id IS NULL) = (current_revision_no IS NULL)",
            name="ck_rfqs_current_revision_pair",
        ),
        Index("ix_rfqs_org_created_at", "org_id", "created_at"),
        ForeignKeyConstraint(
            ["org_id", "rfq_id", "current_revision_id", "current_revision_no"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
                f"{SCHEMA}.rfq_revisions.revision_no",
            ],
            name="fk_rfqs_current_revision",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        {"schema": SCHEMA},
    )

    rfq_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(128))
    current_revision_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RFQRevisionRecord(Base):
    __tablename__ = "rfq_revisions"
    __table_args__ = (
        UniqueConstraint("rfq_id", "revision_no", name="uq_rfq_revisions_number"),
        UniqueConstraint(
            "org_id", "rfq_id", "revision_id", name="uq_rfq_revisions_pointer"
        ),
        UniqueConstraint(
            "org_id",
            "rfq_id",
            "revision_id",
            "revision_no",
            name="uq_rfq_revisions_pointer_no",
        ),
        ForeignKeyConstraint(
            ["org_id", "rfq_id"],
            [f"{SCHEMA}.rfqs.org_id", f"{SCHEMA}.rfqs.rfq_id"],
            name="fk_rfq_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "rfq_id", "parent_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_rfq_revisions_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_no > 0", name="ck_rfq_revisions_number_positive"),
        Index("ix_rfq_revisions_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class QuotationAggregate(Base):
    __tablename__ = "quotations"
    __table_args__ = (
        UniqueConstraint("org_id", "quotation_id", name="uq_quotations_org_id"),
        CheckConstraint(
            "(current_revision_id IS NULL) = (current_revision_no IS NULL)",
            name="ck_quotations_current_revision_pair",
        ),
        Index("ix_quotations_org_created_at", "org_id", "created_at"),
        ForeignKeyConstraint(
            [
                "org_id",
                "quotation_id",
                "current_revision_id",
                "current_revision_no",
            ],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
                f"{SCHEMA}.quote_revisions.revision_no",
            ],
            name="fk_quotations_current_revision",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        {"schema": SCHEMA},
    )

    quotation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(128))
    current_revision_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InquiryCaseRecord(Base):
    """Inbound inquiry identity, SLA deadline, and operational queue state."""

    __tablename__ = "inquiry_cases"
    __table_args__ = (
        UniqueConstraint("org_id", "inquiry_id", name="uq_inquiry_cases_org_id"),
        CheckConstraint(
            "queue_state IN ('open', 'overdue', 'responded', 'nurture', 'closed')",
            name="ck_inquiry_cases_queue_state",
        ),
        CheckConstraint(
            "response_due_at >= received_at",
            name="ck_inquiry_cases_sla_range",
        ),
        ForeignKeyConstraint(
            ["org_id", "rfq_id"],
            [f"{SCHEMA}.rfqs.org_id", f"{SCHEMA}.rfqs.rfq_id"],
            name="fk_inquiry_cases_rfq",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "org_id",
                "inquiry_id",
                "current_reply_revision_id",
                "current_reply_revision_no",
            ],
            [
                f"{SCHEMA}.inquiry_reply_revisions.org_id",
                f"{SCHEMA}.inquiry_reply_revisions.inquiry_id",
                f"{SCHEMA}.inquiry_reply_revisions.reply_revision_id",
                f"{SCHEMA}.inquiry_reply_revisions.revision_no",
            ],
            name="fk_inquiry_cases_current_reply",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "(current_reply_revision_id IS NULL) = (current_reply_revision_no IS NULL)",
            name="ck_inquiry_cases_current_reply_pair",
        ),
        Index("ix_inquiry_cases_org_due", "org_id", "response_due_at", "queue_state"),
        {"schema": SCHEMA},
    )

    inquiry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_role: Mapped[str] = mapped_column(String(32), nullable=False)
    original_language: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    response_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    owner_id: Mapped[str | None] = mapped_column(String(128))
    queue_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    attachments: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    current_reply_revision_id: Mapped[str | None] = mapped_column(String(128))
    current_reply_revision_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class InquiryReplyRevisionRecord(Base):
    """Immutable customer reply snapshot for one inquiry and RFQ revision."""

    __tablename__ = "inquiry_reply_revisions"
    __table_args__ = (
        UniqueConstraint(
            "inquiry_id", "revision_no", name="uq_inquiry_reply_revisions_number"
        ),
        UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            name="uq_inquiry_reply_revisions_pointer",
        ),
        UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            "revision_no",
            name="uq_inquiry_reply_revisions_pointer_no",
        ),
        ForeignKeyConstraint(
            ["org_id", "inquiry_id"],
            [
                f"{SCHEMA}.inquiry_cases.org_id",
                f"{SCHEMA}.inquiry_cases.inquiry_id",
            ],
            name="fk_inquiry_reply_revisions_inquiry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "rfq_id", "rfq_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_inquiry_reply_revisions_rfq_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "inquiry_id", "parent_reply_revision_id"],
            [
                f"{SCHEMA}.inquiry_reply_revisions.org_id",
                f"{SCHEMA}.inquiry_reply_revisions.inquiry_id",
                f"{SCHEMA}.inquiry_reply_revisions.reply_revision_id",
            ],
            name="fk_inquiry_reply_revisions_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "revision_no > 0", name="ck_inquiry_reply_revisions_number_positive"
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_inquiry_reply_revisions_hash_length",
        ),
        Index("ix_inquiry_reply_revisions_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    reply_revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    inquiry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_reply_revision_id: Mapped[str | None] = mapped_column(String(128))
    rfq_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_language: Mapped[str] = mapped_column(String(16), nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    source_content: Mapped[str] = mapped_column(String(20000), nullable=False)
    translated_content: Mapped[str | None] = mapped_column(String(20000))
    template_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="reply-v1"
    )
    attachments: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class InquiryTranslationReviewRecord(Base):
    """One immutable human translation decision bound to a reply hash."""

    __tablename__ = "inquiry_translation_reviews"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            name="uq_inquiry_translation_reviews_revision",
        ),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_inquiry_translation_reviews_idempotency",
        ),
        ForeignKeyConstraint(
            ["org_id", "inquiry_id", "reply_revision_id"],
            [
                f"{SCHEMA}.inquiry_reply_revisions.org_id",
                f"{SCHEMA}.inquiry_reply_revisions.inquiry_id",
                f"{SCHEMA}.inquiry_reply_revisions.reply_revision_id",
            ],
            name="fk_inquiry_translation_reviews_reply",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_inquiry_translation_reviews_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_inquiry_translation_reviews_decision",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_inquiry_translation_reviews_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_inquiry_translation_reviews_request_hash_length",
        ),
        Index(
            "ix_inquiry_translation_reviews_org_reviewed_at",
            "org_id",
            "reviewed_at",
        ),
        {"schema": SCHEMA},
    )

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    inquiry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reply_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class QualificationCheckRecord(Base):
    """Immutable buyer or inquiry screening evidence snapshot."""

    __tablename__ = "qualification_checks"
    __table_args__ = (
        CheckConstraint(
            "customer_id IS NOT NULL OR inquiry_id IS NOT NULL",
            name="ck_qualification_checks_target",
        ),
        CheckConstraint(
            "result IN ('clear', 'potential_match', 'blocked', 'unverified')",
            name="ck_qualification_checks_result",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_qualification_checks_evidence_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_qualification_checks_request_hash_length",
        ),
        ForeignKeyConstraint(
            ["org_id", "inquiry_id"],
            [
                f"{SCHEMA}.inquiry_cases.org_id",
                f"{SCHEMA}.inquiry_cases.inquiry_id",
            ],
            name="fk_qualification_checks_inquiry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_qualification_checks_idempotency",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "check_id", name="uq_qualification_checks_org_id"),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_qualification_checks_idempotency",
        ),
        UniqueConstraint(
            "idempotency_record_id", name="uq_qualification_checks_idempotency_record"
        ),
        Index(
            "ix_qualification_checks_org_checked_at",
            "org_id",
            "checked_at",
        ),
        Index(
            "ix_qualification_checks_org_customer",
            "org_id",
            "customer_id",
        ),
        Index(
            "ix_qualification_checks_org_inquiry",
            "org_id",
            "inquiry_id",
        ),
        {"schema": SCHEMA},
    )

    check_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(128))
    inquiry_id: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_attachment_ref: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(String(4000))
    reviewer_id: Mapped[str | None] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class QualificationDecisionRecord(Base):
    """Immutable reviewer decision bound to one screening evidence hash."""

    __tablename__ = "qualification_decisions"
    __table_args__ = (
        CheckConstraint(
            "result IN ('clear', 'potential_match', 'blocked', 'unverified')",
            name="ck_qualification_decisions_result",
        ),
        CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_qualification_decisions_evidence_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_qualification_decisions_request_hash_length",
        ),
        ForeignKeyConstraint(
            ["org_id", "check_id"],
            [
                f"{SCHEMA}.qualification_checks.org_id",
                f"{SCHEMA}.qualification_checks.check_id",
            ],
            name="fk_qualification_decisions_check",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_qualification_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "check_id", name="uq_qualification_decisions_check"),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_qualification_decisions_idempotency",
        ),
        UniqueConstraint(
            "idempotency_record_id",
            name="uq_qualification_decisions_idempotency_record",
        ),
        Index(
            "ix_qualification_decisions_org_decided_at",
            "org_id",
            "decided_at",
        ),
        {"schema": SCHEMA},
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    check_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(4000))
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class QuoteRevisionRecord(Base):
    __tablename__ = "quote_revisions"
    __table_args__ = (
        UniqueConstraint(
            "quotation_id", "revision_no", name="uq_quote_revisions_number"
        ),
        UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            name="uq_quote_revisions_pointer",
        ),
        UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            "revision_no",
            name="uq_quote_revisions_pointer_no",
        ),
        UniqueConstraint(
            "run_id",
            "action_type",
            "business_version",
            name="uq_quote_revisions_run_action_version",
        ),
        ForeignKeyConstraint(
            ["org_id", "quotation_id"],
            [f"{SCHEMA}.quotations.org_id", f"{SCHEMA}.quotations.quotation_id"],
            name="fk_quote_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "quotation_id", "parent_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_quote_revisions_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_no > 0", name="ck_quote_revisions_number_positive"),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_quote_revisions_hash_length"
        ),
        Index("ix_quote_revisions_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[str | None] = mapped_column(String(128))
    action_type: Mapped[str | None] = mapped_column(String(64))
    business_version: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class QuoteApprovalRecord(Base):
    """One immutable reviewer decision bound to one quote content hash."""

    __tablename__ = "quote_approvals"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            name="uq_quote_approvals_revision",
        ),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_quote_approvals_idempotency",
        ),
        UniqueConstraint(
            "org_id", "approval_id", name="uq_quote_approvals_org_id_approval"
        ),
        ForeignKeyConstraint(
            ["org_id", "quotation_id", "revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_quote_approvals_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_quote_approvals_decision",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_quote_approvals_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_quote_approvals_request_hash_length",
        ),
        Index("ix_quote_approvals_org_quote", "org_id", "quotation_id"),
        {"schema": SCHEMA},
    )

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class QuoteArtifactRecord(Base):
    """Private, reproducible file generated from an approved quote snapshot."""

    __tablename__ = "quote_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            "artifact_type",
            name="uq_quote_artifacts_revision_type",
        ),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_quote_artifacts_idempotency",
        ),
        ForeignKeyConstraint(
            ["approval_id"],
            [f"{SCHEMA}.quote_approvals.approval_id"],
            name="fk_quote_artifacts_approval",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name="ck_quote_artifacts_status",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_quote_artifacts_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_quote_artifacts_request_hash_length",
        ),
        CheckConstraint(
            "amount_total >= 0",
            name="ck_quote_artifacts_amount_nonnegative",
        ),
        Index("ix_quote_artifacts_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DocumentSetRecord(Base):
    """Organization-scoped shipment-document aggregate anchored to an approval."""

    __tablename__ = "document_sets"
    __table_args__ = (
        UniqueConstraint("org_id", "document_set_id", name="uq_document_sets_org_id"),
        ForeignKeyConstraint(
            ["org_id", "quotation_id", "quote_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_document_sets_quote_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "approval_id"],
            [
                f"{SCHEMA}.quote_approvals.org_id",
                f"{SCHEMA}.quote_approvals.approval_id",
            ],
            name="fk_document_sets_quote_approval",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('draft', 'ready', 'issued', 'void')",
            name="ck_document_sets_status",
        ),
        CheckConstraint(
            "length(approved_content_hash) = 64",
            name="ck_document_sets_approved_hash_length",
        ),
        Index("ix_document_sets_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    document_set_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(128))
    current_revision_no: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class DocumentSetRevisionRecord(Base):
    """Append-only commercial-invoice or packing-list snapshot."""

    __tablename__ = "document_set_revisions"
    __table_args__ = (
        UniqueConstraint(
            "document_set_id", "revision_no", name="uq_document_set_revisions_number"
        ),
        UniqueConstraint(
            "org_id",
            "document_set_id",
            "revision_id",
            name="uq_document_set_revisions_pointer",
        ),
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_document_set_revisions_idempotency",
        ),
        ForeignKeyConstraint(
            ["org_id", "document_set_id"],
            [
                f"{SCHEMA}.document_sets.org_id",
                f"{SCHEMA}.document_sets.document_set_id",
            ],
            name="fk_document_set_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "document_set_id", "parent_revision_id"],
            [
                f"{SCHEMA}.document_set_revisions.org_id",
                f"{SCHEMA}.document_set_revisions.document_set_id",
                f"{SCHEMA}.document_set_revisions.revision_id",
            ],
            name="fk_document_set_revisions_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_document_set_revisions_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "document_type IN ('commercial_invoice', 'packing_list')",
            name="ck_document_set_revisions_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'ready', 'issued', 'void')",
            name="ck_document_set_revisions_status",
        ),
        CheckConstraint(
            "revision_no > 0", name="ck_document_set_revisions_number_positive"
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_document_set_revisions_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_document_set_revisions_request_hash_length",
        ),
        Index("ix_document_set_revisions_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(128))
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ShipmentHandoffRecord(Base):
    """Organization-scoped shipment handoff anchored to an approved quote."""

    __tablename__ = "shipment_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "shipment_handoff_id", name="uq_shipment_handoffs_org_id"
        ),
        ForeignKeyConstraint(
            ["org_id", "quotation_id", "quote_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_shipment_handoffs_quote_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "approval_id"],
            [
                f"{SCHEMA}.quote_approvals.org_id",
                f"{SCHEMA}.quote_approvals.approval_id",
            ],
            name="fk_shipment_handoffs_quote_approval",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "document_set_id"],
            [
                f"{SCHEMA}.document_sets.org_id",
                f"{SCHEMA}.document_sets.document_set_id",
            ],
            name="fk_shipment_handoffs_document_set",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('draft', 'ready_for_booking', 'booked', 'in_transit', 'completed', 'cancelled', 'blocked')",
            name="ck_shipment_handoffs_status",
        ),
        CheckConstraint(
            "length(approved_content_hash) = 64",
            name="ck_shipment_handoffs_approved_hash_length",
        ),
        CheckConstraint(
            "length(trim(selected_incoterm)) > 0",
            name="ck_shipment_handoffs_incoterm_nonempty",
        ),
        CheckConstraint(
            "length(trim(named_place)) > 0",
            name="ck_shipment_handoffs_named_place_nonempty",
        ),
        Index("ix_shipment_handoffs_org_created_at", "org_id", "created_at"),
        Index("ix_shipment_handoffs_org_status", "org_id", "status"),
        {"schema": SCHEMA},
    )

    shipment_handoff_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_set_id: Mapped[str | None] = mapped_column(String(128))
    selected_incoterm: Mapped[str] = mapped_column(String(32), nullable=False)
    named_place: Mapped[str] = mapped_column(String(128), nullable=False)
    responsibility_split: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    freight_forwarder_name: Mapped[str | None] = mapped_column(String(255))
    freight_forwarder_quote_ref: Mapped[str | None] = mapped_column(String(512))
    carrier_name: Mapped[str | None] = mapped_column(String(255))
    insurance_scope: Mapped[str | None] = mapped_column(String(2000))
    insurance_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    booking_reference: Mapped[str | None] = mapped_column(String(255))
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    required_documents: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ShipmentHandoffEvidenceRecord(Base):
    """Immutable evidence/checklist snapshot for one shipment handoff."""

    __tablename__ = "shipment_handoff_evidence"
    __table_args__ = (
        UniqueConstraint(
            "shipment_handoff_id",
            "revision_no",
            name="uq_shipment_handoff_evidence_revision",
        ),
        UniqueConstraint(
            "org_id",
            "shipment_handoff_id",
            "evidence_id",
            name="uq_shipment_handoff_evidence_pointer",
        ),
        ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id"],
            [
                f"{SCHEMA}.shipment_handoffs.org_id",
                f"{SCHEMA}.shipment_handoffs.shipment_handoff_id",
            ],
            name="fk_shipment_handoff_evidence_aggregate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id", "parent_evidence_id"],
            [
                f"{SCHEMA}.shipment_handoff_evidence.org_id",
                f"{SCHEMA}.shipment_handoff_evidence.shipment_handoff_id",
                f"{SCHEMA}.shipment_handoff_evidence.evidence_id",
            ],
            name="fk_shipment_handoff_evidence_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_shipment_handoff_evidence_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "revision_no > 0", name="ck_shipment_handoff_evidence_revision_positive"
        ),
        CheckConstraint(
            "evidence_type IN ('freight_quote', 'insurance', 'packing_check', 'label_check', 'required_document', 'booking', 'bill_of_lading', 'air_waybill', 'export_document', 'import_document', 'other')",
            name="ck_shipment_handoff_evidence_type",
        ),
        CheckConstraint(
            "status IN ('verified', 'pending', 'rejected', 'void')",
            name="ck_shipment_handoff_evidence_status",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_shipment_handoff_evidence_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_shipment_handoff_evidence_request_hash_length",
        ),
        Index("ix_shipment_handoff_evidence_org_created_at", "org_id", "created_at"),
        Index(
            "ix_shipment_handoff_evidence_aggregate_key",
            "org_id",
            "shipment_handoff_id",
            "evidence_type",
            "check_key",
            "revision_no",
        ),
        {"schema": SCHEMA},
    )

    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shipment_handoff_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_evidence_id: Mapped[str | None] = mapped_column(String(128))
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    check_key: Mapped[str | None] = mapped_column(String(128))
    evidence_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="verified"
    )
    notes: Mapped[str | None] = mapped_column(String(4000))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ShipmentHandoffGateDecisionRecord(Base):
    """Immutable reviewer/admin decision over a shipment gate."""

    __tablename__ = "shipment_handoff_gate_decisions"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "shipment_handoff_id",
            "target_status",
            "decision_id",
            name="uq_shipment_handoff_gate_decision_pointer",
        ),
        ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id"],
            [
                f"{SCHEMA}.shipment_handoffs.org_id",
                f"{SCHEMA}.shipment_handoffs.shipment_handoff_id",
            ],
            name="fk_shipment_handoff_gate_decisions_aggregate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_shipment_handoff_gate_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "target_status IN ('ready_for_booking', 'booked')",
            name="ck_shipment_handoff_gate_target",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_shipment_handoff_gate_decision",
        ),
        CheckConstraint(
            "actor_role IN ('reviewer', 'admin')",
            name="ck_shipment_handoff_gate_actor_role",
        ),
        CheckConstraint(
            "length(gate_hash) = 64", name="ck_shipment_handoff_gate_hash_length"
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_shipment_handoff_gate_request_hash_length",
        ),
        Index("ix_shipment_handoff_gate_decisions_org_time", "org_id", "decided_at"),
        {"schema": SCHEMA},
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shipment_handoff_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    gate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    reason: Mapped[str | None] = mapped_column(String(4000))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Backward-compatible short names for service and API callers.
ShipmentEvidenceRecord = ShipmentHandoffEvidenceRecord
ShipmentGateDecisionRecord = ShipmentHandoffGateDecisionRecord


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
        CheckConstraint("length(request_hash) = 64", name="ck_idempotency_hash_length"),
        Index("ix_idempotency_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    record_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_aggregate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    result_aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    result_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_audit_events_idempotency",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_record_id", name="uq_audit_events_idempotency"),
        CheckConstraint(
            "aggregate_type IN ('rfq', 'quotation')",
            name="ck_audit_events_aggregate_type",
        ),
        Index(
            "ix_audit_events_org_aggregate_time",
            "org_id",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        {"schema": SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_revision_id: Mapped[str | None] = mapped_column(String(128))
    revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MatchDecisionRecord(Base):
    __tablename__ = "match_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "rfq_id", "rfq_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_match_decisions_rfq_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["catalog_version", "selected_sku"],
            [f"{SCHEMA}.products.catalog_version", f"{SCHEMA}.products.sku"],
            name="fk_match_decisions_product",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_match_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "idempotency_record_id", name="uq_match_decisions_idempotency"
        ),
        CheckConstraint("length(trim(reason)) > 0", name="ck_match_decisions_reason"),
        Index(
            "ix_match_decisions_rfq_item_time",
            "org_id",
            "rfq_id",
            "rfq_revision_id",
            "rfq_item_id",
            "created_at",
        ),
        {"schema": SCHEMA},
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_sku: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RunRecord(Base):
    """Durable lifecycle and worker lease for one workflow execution."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        CheckConstraint("event_seq >= 0", name="ck_runs_event_seq_nonnegative"),
        UniqueConstraint("org_id", "run_id", name="uq_runs_org_id"),
        Index("ix_runs_status_updated_at", "status", "updated_at"),
        {"schema": SCHEMA},
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rfq_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(String(32))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RunEventRecord(Base):
    """Append-only operational event stream for a run."""

    __tablename__ = "run_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            [f"{SCHEMA}.runs.run_id"],
            name="fk_run_events_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "event_seq", name="uq_run_events_sequence"),
        UniqueConstraint("run_id", "event_key", name="uq_run_events_key"),
        Index("ix_run_events_run_seq", "run_id", "event_seq"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentTermsPlanRecord(Base):
    """Reviewer-confirmed payment terms bound to one quote snapshot."""

    __tablename__ = "payment_terms_plans"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "quotation_id", "quote_revision_id",
            name="uq_payment_terms_plan_quote",
        ),
        ForeignKeyConstraint(
            ["org_id", "quotation_id", "quote_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_payment_terms_plan_quote",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "payment_method IN ('cash_in_advance', 'letter_of_credit', 'documentary_collection', 'open_account', 'consignment', 'other')",
            name="ck_payment_terms_plan_method",
        ),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'blocked', 'cancelled')",
            name="ck_payment_terms_plan_status",
        ),
        CheckConstraint(
            "deposit_percent IS NULL OR (deposit_percent >= 0 AND deposit_percent <= 100)",
            name="ck_payment_terms_plan_deposit_range",
        ),
        CheckConstraint(
            "length(quote_content_hash) = 64",
            name="ck_payment_terms_plan_quote_hash_length",
        ),
        Index("ix_payment_terms_plans_org_status", "org_id", "status"),
        {"schema": SCHEMA},
    )

    payment_plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_revision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    terms_summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    deposit_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    balance_condition: Mapped[str] = mapped_column(String(1000), nullable=False)
    requires_insurance_or_guarantee: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentRiskEvidenceRecord(Base):
    """Append-only insurance/guarantee evidence for payment protection."""

    __tablename__ = "payment_risk_evidence"
    __table_args__ = (
        UniqueConstraint(
            "payment_plan_id", "revision_no",
            name="uq_payment_risk_evidence_revision",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_plan_id"],
            [
                f"{SCHEMA}.payment_terms_plans.org_id",
                f"{SCHEMA}.payment_terms_plans.payment_plan_id",
            ],
            name="fk_payment_risk_evidence_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_payment_risk_evidence_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "evidence_type IN ('insurance', 'guarantee', 'other')",
            name="ck_payment_risk_evidence_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'verified', 'rejected', 'void')",
            name="ck_payment_risk_evidence_status",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_payment_risk_evidence_hash_length",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_payment_risk_evidence_request_hash_length",
        ),
        Index("ix_payment_risk_evidence_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String(4000))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentDueTaskRecord(Base):
    """Mutable task projection; every lifecycle change has an immutable event."""

    __tablename__ = "payment_due_tasks"
    __table_args__ = (
        UniqueConstraint("org_id", "task_id", name="uq_payment_due_tasks_org_id"),
        ForeignKeyConstraint(
            ["org_id", "payment_plan_id"],
            [
                f"{SCHEMA}.payment_terms_plans.org_id",
                f"{SCHEMA}.payment_terms_plans.payment_plan_id",
            ],
            name="fk_payment_due_tasks_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "task_kind IN ('deposit', 'balance', 'other')",
            name="ck_payment_due_tasks_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')",
            name="ck_payment_due_tasks_status",
        ),
        CheckConstraint("amount > 0", name="ck_payment_due_tasks_amount_positive"),
        Index("ix_payment_due_tasks_org_due", "org_id", "due_at", "status"),
        {"schema": SCHEMA},
    )

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    condition: Mapped[str] = mapped_column(String(1000), nullable=False)
    blocks_shipment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[str | None] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentDueTaskEventRecord(Base):
    """Immutable audit event for payment task creation/completion/cancellation."""

    __tablename__ = "payment_due_task_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "task_id"],
            [
                f"{SCHEMA}.payment_due_tasks.org_id",
                f"{SCHEMA}.payment_due_tasks.task_id",
            ],
            name="fk_payment_due_task_events_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_payment_due_task_events_idempotency",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('created', 'completed', 'cancelled')",
            name="ck_payment_due_task_events_type",
        ),
        Index("ix_payment_due_task_events_org_created_at", "org_id", "created_at"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000))
    idempotency_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
