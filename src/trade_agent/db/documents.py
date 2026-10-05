"""Hash-bound, append-only commercial invoice and packing-list snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select, text

from trade_agent.contracts import ExportDocumentType
from trade_agent.db.models import (
    DocumentSetRecord,
    DocumentSetRevisionRecord,
    IdempotencyRecord,
    InquiryCaseRecord,
    QuoteApprovalRecord,
    QuoteRevisionRecord,
    RFQRevisionRecord,
)
from trade_agent.db.qualification import (
    QualificationGateError,
    ensure_quote_qualification_clear,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.contracts import CommercialInvoiceDocument, PackingListDocument


class DocumentError(Exception):
    """Base class for document consistency failures."""


class DocumentNotFoundError(DocumentError):
    """The document set is not visible in the requested organization."""


class DocumentConflictError(DocumentError):
    """The requested document operation conflicts with its current state."""


class DocumentIdempotencyConflictError(DocumentConflictError):
    """An idempotency key was reused with another request."""


class DocumentHashMismatchError(DocumentConflictError):
    """The approved quote hash no longer matches the displayed snapshot."""


class DocumentNotApprovedError(DocumentConflictError):
    """The referenced quote revision has no matching approved decision."""


class DocumentValidationError(DocumentConflictError):
    """A document set has deterministic field-level blockers."""

    def __init__(self, issues: tuple[DocumentIssue, ...]) -> None:
        self.issues = issues
        super().__init__("document set validation failed")


@dataclass(frozen=True, slots=True)
class DocumentIssue:
    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class DocumentSetResult:
    document_set_id: str
    quotation_id: str
    quote_revision_id: str
    approved_content_hash: str
    status: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class DocumentRevisionResult:
    document_set_id: str
    revision_id: str
    revision_no: int
    document_type: str
    status: str
    content_hash: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class DocumentValidationResult:
    document_set_id: str
    valid: bool
    ready: bool
    issues: tuple[DocumentIssue, ...]


def create_document_set(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    document_set_id: str,
    quotation_id: str,
    quote_revision_id: str,
    approved_content_hash: str,
    actor_id: str,
    idempotency_key: str,
    created_at: datetime | None = None,
) -> DocumentSetResult:
    """Create an aggregate only when the exact quote revision is approved."""
    now = created_at or datetime.now(UTC)
    request_hash = _request_hash({
        "approved_content_hash": approved_content_hash,
        "document_set_id": document_set_id,
        "quote_revision_id": quote_revision_id,
        "quotation_id": quotation_id,
    })
    with session.begin_nested():
        _lock_idempotency_scope(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        existing = _get_idempotency(
            session, org_id=org_id, actor_id=actor_id, key=idempotency_key
        )
        if existing is not None:
            _check_idempotency(existing, "document_sets.create", request_hash)
            record = session.get(DocumentSetRecord, existing.result_revision_id)
            if record is None or record.org_id != org_id:
                raise DocumentConflictError("idempotency record references missing set")
            return _set_result(record, idempotent_replay=True)

        duplicate = session.scalar(
            select(DocumentSetRecord).where(
                DocumentSetRecord.document_set_id == document_set_id
            )
        )
        if duplicate is not None:
            if (
                duplicate.org_id != org_id
                or duplicate.quotation_id != quotation_id
                or duplicate.quote_revision_id != quote_revision_id
                or duplicate.approved_content_hash != approved_content_hash
            ):
                raise DocumentConflictError("document_set_id targets another snapshot")
            raise DocumentConflictError("document_set_id already exists")

        quote = _approved_quote(
            session,
            org_id=org_id,
            quotation_id=quotation_id,
            quote_revision_id=quote_revision_id,
            approved_content_hash=approved_content_hash,
        )
        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint="document_sets.create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type="document_set",
                result_aggregate_id=document_set_id,
                result_revision_id=document_set_id,
                created_at=now,
            )
        )
        session.add(
            DocumentSetRecord(
                document_set_id=document_set_id,
                org_id=org_id,
                quotation_id=quotation_id,
                quote_revision_id=quote_revision_id,
                approval_id=quote.approval_id,
                approved_content_hash=approved_content_hash,
                status="draft",
                created_at=now,
                updated_at=now,
                created_by=actor_id,
            )
        )
        session.flush()
        record = session.get(DocumentSetRecord, document_set_id)
        if record is None:
            raise DocumentConflictError("document set could not be created")
        return _set_result(record, idempotent_replay=False)


def append_document_revision(  # noqa: C901, PLR0913
    session: Session,
    *,
    org_id: str,
    document_set_id: str,
    document_type: ExportDocumentType | str,
    document: CommercialInvoiceDocument | PackingListDocument,
    actor_id: str,
    idempotency_key: str,
    expected_revision_id: str | None = None,
    status: str = "draft",
    created_at: datetime | None = None,
) -> DocumentRevisionResult:
    """Append a deterministic snapshot; existing revisions are never updated.

    Partial shipments are explicit: each invoice and package row must be a
    positive quantity no greater than its approved quote line. The latest
    commercial-invoice and packing-list totals must then match each other
    exactly before the set can become ready or issued.
    """
    dtype = ExportDocumentType(document_type).value
    if status not in {"draft", "ready", "issued", "void"}:
        raise ValueError("invalid document revision status")
    snapshot = document.model_dump(mode="json")
    request_hash = _request_hash({
        "document": snapshot,
        "document_set_id": document_set_id,
        "document_type": dtype,
        "expected_revision_id": expected_revision_id,
        "status": status,
    })
    content_hash = _content_hash(dtype, snapshot)
    now = created_at or datetime.now(UTC)
    with session.begin_nested():
        _lock_idempotency_scope(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        existing = _get_idempotency(
            session, org_id=org_id, actor_id=actor_id, key=idempotency_key
        )
        if existing is not None:
            _check_idempotency(existing, "document_sets.revisions.append", request_hash)
            revision = session.get(
                DocumentSetRevisionRecord, existing.result_revision_id
            )
            if revision is None or revision.org_id != org_id:
                raise DocumentConflictError(
                    "idempotency record references missing revision"
                )
            return _revision_result(revision, idempotent_replay=True)

        record = session.scalar(
            select(DocumentSetRecord)
            .where(
                DocumentSetRecord.org_id == org_id,
                DocumentSetRecord.document_set_id == document_set_id,
            )
            .with_for_update()
        )
        if record is None:
            raise DocumentNotFoundError(document_set_id)
        if record.status == "void":
            raise DocumentConflictError("a void document set cannot be revised")
        _approved_quote(
            session,
            org_id=org_id,
            quotation_id=record.quotation_id,
            quote_revision_id=record.quote_revision_id,
            approved_content_hash=record.approved_content_hash,
        )
        if expected_revision_id != record.current_revision_id:
            raise DocumentConflictError(
                "current document revision does not match expectation"
            )
        quote = session.scalar(
            select(QuoteRevisionRecord).where(
                QuoteRevisionRecord.org_id == org_id,
                QuoteRevisionRecord.quotation_id == record.quotation_id,
                QuoteRevisionRecord.revision_id == record.quote_revision_id,
            )
        )
        if quote is None:
            raise DocumentNotFoundError(record.quote_revision_id)
        issues = _validate_single(dtype, snapshot, quote.payload, quote.content_hash)
        if status in {"ready", "issued"} and issues:
            raise DocumentValidationError(tuple(issues))
        revision_no = (record.current_revision_no or 0) + 1
        revision_id = str(uuid.uuid4())
        idem_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=idem_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint="document_sets.revisions.append",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type="document_set",
                result_aggregate_id=document_set_id,
                result_revision_id=revision_id,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            DocumentSetRevisionRecord(
                revision_id=revision_id,
                org_id=org_id,
                document_set_id=document_set_id,
                revision_no=revision_no,
                parent_revision_id=record.current_revision_id,
                document_type=dtype,
                status=status,
                content_hash=content_hash,
                snapshot=snapshot,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                idempotency_record_id=idem_id,
                created_at=now,
            )
        )
        record.current_revision_id = revision_id
        record.current_revision_no = revision_no
        record.status = "draft" if status == "draft" else status
        record.updated_at = now
        session.flush()
        if status in {"ready", "issued"}:
            checked = validate_document_set(
                session, org_id=org_id, document_set_id=document_set_id
            )
            if not checked.ready:
                raise DocumentValidationError(checked.issues)
        revision = session.get(DocumentSetRevisionRecord, revision_id)
        if revision is None:
            raise DocumentConflictError("document revision could not be created")
        return _revision_result(revision, idempotent_replay=False)


def get_document_set(
    session: Session, *, org_id: str, document_set_id: str
) -> DocumentSetRecord | None:
    """Return a set only when it belongs to the caller organization."""
    return session.scalar(
        select(DocumentSetRecord).where(
            DocumentSetRecord.org_id == org_id,
            DocumentSetRecord.document_set_id == document_set_id,
        )
    )


def validate_document_set(
    session: Session, *, org_id: str, document_set_id: str
) -> DocumentValidationResult:
    """Compare both latest document types to the approved quote snapshot."""
    record = get_document_set(session, org_id=org_id, document_set_id=document_set_id)
    if record is None:
        raise DocumentNotFoundError(document_set_id)
    quote = session.scalar(
        select(QuoteRevisionRecord).where(
            QuoteRevisionRecord.org_id == org_id,
            QuoteRevisionRecord.quotation_id == record.quotation_id,
            QuoteRevisionRecord.revision_id == record.quote_revision_id,
        )
    )
    if quote is None:
        raise DocumentNotFoundError(record.quote_revision_id)
    _approved_quote(
        session,
        org_id=org_id,
        quotation_id=record.quotation_id,
        quote_revision_id=record.quote_revision_id,
        approved_content_hash=record.approved_content_hash,
    )
    revisions = list(
        session.scalars(
            select(DocumentSetRevisionRecord).where(
                DocumentSetRevisionRecord.org_id == org_id,
                DocumentSetRevisionRecord.document_set_id == document_set_id,
            )
        )
    )
    latest: dict[str, DocumentSetRevisionRecord] = {}
    for revision in revisions:
        current = latest.get(revision.document_type)
        if current is None or revision.revision_no > current.revision_no:
            latest[revision.document_type] = revision
    issues: list[DocumentIssue] = []
    for dtype in ExportDocumentType:
        revision = latest.get(dtype.value)
        if revision is None:
            issues.append(
                DocumentIssue(
                    "document_missing", dtype.value, f"{dtype.value} is required"
                )
            )
            continue
        issues.extend(
            _validate_single(
                dtype.value, revision.snapshot, quote.payload, quote.content_hash
            )
        )
        if revision.status == "void":
            issues.append(
                DocumentIssue("document_void", dtype.value, "latest revision is void")
            )
    ci = latest.get(ExportDocumentType.COMMERCIAL_INVOICE.value)
    pl = latest.get(ExportDocumentType.PACKING_LIST.value)
    if ci is not None and pl is not None:
        issues.extend(_validate_cross_document(ci.snapshot, pl.snapshot))
    valid = not issues
    return DocumentValidationResult(
        document_set_id=document_set_id,
        valid=valid,
        ready=valid and record.status != "void",
        issues=tuple(issues),
    )


def issue_document_set(
    session: Session, *, org_id: str, document_set_id: str
) -> DocumentSetResult:
    """Mark a fully validated set issued; snapshots remain immutable."""
    record = get_document_set(session, org_id=org_id, document_set_id=document_set_id)
    if record is None:
        raise DocumentNotFoundError(document_set_id)
    result = validate_document_set(
        session, org_id=org_id, document_set_id=document_set_id
    )
    if not result.ready:
        raise DocumentValidationError(result.issues)
    record.status = "issued"
    record.updated_at = datetime.now(UTC)
    session.flush()
    return _set_result(record, idempotent_replay=False)


def ready_document_set(
    session: Session, *, org_id: str, document_set_id: str
) -> DocumentSetResult:
    """Record the ready gate after both document types pass validation."""
    record = get_document_set(session, org_id=org_id, document_set_id=document_set_id)
    if record is None:
        raise DocumentNotFoundError(document_set_id)
    result = validate_document_set(
        session, org_id=org_id, document_set_id=document_set_id
    )
    if not result.ready:
        raise DocumentValidationError(result.issues)
    record.status = "ready"
    record.updated_at = datetime.now(UTC)
    session.flush()
    return _set_result(record, idempotent_replay=False)


def void_document_set(
    session: Session, *, org_id: str, document_set_id: str
) -> DocumentSetResult:
    """Void an issued set so a correction must be a new revision/set."""
    record = get_document_set(session, org_id=org_id, document_set_id=document_set_id)
    if record is None:
        raise DocumentNotFoundError(document_set_id)
    if record.status == "void":
        return _set_result(record, idempotent_replay=True)
    record.status = "void"
    record.updated_at = datetime.now(UTC)
    session.flush()
    return _set_result(record, idempotent_replay=False)


@dataclass(frozen=True, slots=True)
class _ApprovedQuote:
    approval_id: str


def _approved_quote(
    session: Session,
    *,
    org_id: str,
    quotation_id: str,
    quote_revision_id: str,
    approved_content_hash: str,
) -> _ApprovedQuote:
    quote = session.scalar(
        select(QuoteRevisionRecord).where(
            QuoteRevisionRecord.org_id == org_id,
            QuoteRevisionRecord.quotation_id == quotation_id,
            QuoteRevisionRecord.revision_id == quote_revision_id,
        )
    )
    if quote is None:
        raise DocumentNotFoundError("quote revision not found")
    if quote.content_hash != approved_content_hash:
        raise DocumentHashMismatchError("approved quote hash does not match revision")
    approval = session.scalar(
        select(QuoteApprovalRecord).where(
            QuoteApprovalRecord.org_id == org_id,
            QuoteApprovalRecord.quotation_id == quotation_id,
            QuoteApprovalRecord.revision_id == quote_revision_id,
        )
    )
    if approval is None or approval.decision != "approved":
        raise DocumentNotApprovedError("only an approved quote can anchor documents")
    if approval.content_hash != approved_content_hash:
        raise DocumentHashMismatchError("approval hash does not match revision")
    _ensure_document_qualification_clear(session, org_id=org_id, quote=quote)
    return _ApprovedQuote(approval_id=approval.approval_id)


def _ensure_document_qualification_clear(
    session: Session, *, org_id: str, quote: QuoteRevisionRecord
) -> None:
    rfq_revision_id = quote.payload.get("rfq_revision_id")
    if not isinstance(rfq_revision_id, str) or not rfq_revision_id:
        raise QualificationGateError(
            "document issuance requires a linked RFQ qualification target"
        )
    rfq = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == org_id,
            RFQRevisionRecord.revision_id == rfq_revision_id,
        )
    )
    if rfq is None:
        raise QualificationGateError(
            "document issuance requires a visible RFQ qualification target"
        )
    plan = rfq.payload.get("plan")
    customer_id = plan.get("customer_id") if isinstance(plan, dict) else None
    inquiries = list(
        session.scalars(
            select(InquiryCaseRecord.inquiry_id).where(
                InquiryCaseRecord.org_id == org_id,
                InquiryCaseRecord.rfq_id == rfq.rfq_id,
            )
        )
    )
    if customer_id is None and not inquiries:
        raise QualificationGateError(
            "document issuance requires a customer or inquiry qualification target"
        )
    ensure_quote_qualification_clear(
        session, org_id=org_id, rfq_revision_id=rfq_revision_id
    )


def _quote_lines(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lines = payload.get("items")
    if not isinstance(lines, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in lines:
        if isinstance(line, dict) and isinstance(line.get("quote_item_id"), str):
            result[line["quote_item_id"]] = line
    return result


def _validate_single(  # noqa: C901, PLR0912, PLR0915
    document_type: str,
    snapshot: dict[str, Any],
    quote_payload: dict[str, Any],
    quote_hash: str | None,
) -> list[DocumentIssue]:
    del quote_hash
    issues: list[DocumentIssue] = []
    quote_lines = _quote_lines(quote_payload)
    expected_currency = str(quote_payload.get("currency", ""))
    expected_term = quote_payload.get("trade_term")
    expected_place = quote_payload.get("named_place")
    if not str(snapshot.get("buyer", "")).strip():
        issues.append(DocumentIssue("missing_buyer", "buyer", "buyer is required"))
    if not str(snapshot.get("consignee", "")).strip():
        issues.append(
            DocumentIssue("missing_consignee", "consignee", "consignee is required")
        )
    if not str(snapshot.get("document_reference", "")).strip():
        issues.append(
            DocumentIssue(
                "missing_reference",
                "document_reference",
                "document reference is required",
            )
        )
    if snapshot.get("incoterm") != expected_term:
        issues.append(
            DocumentIssue(
                "incoterm_mismatch", "incoterm", "Incoterm differs from approved quote"
            )
        )
    if snapshot.get("named_place") != expected_place:
        issues.append(
            DocumentIssue(
                "named_place_mismatch",
                "named_place",
                "named place differs from approved quote",
            )
        )
    if document_type == ExportDocumentType.COMMERCIAL_INVOICE.value:
        if snapshot.get("currency") != expected_currency:
            issues.append(
                DocumentIssue(
                    "currency_mismatch",
                    "currency",
                    "currency differs from approved quote",
                )
            )
        lines = snapshot.get("lines")
        if not isinstance(lines, list) or not lines:
            issues.append(
                DocumentIssue("missing_lines", "lines", "invoice lines are required")
            )
            return issues
        supplied: set[str] = set()
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                issues.append(
                    DocumentIssue(
                        "invalid_line", f"lines[{index}]", "line must be an object"
                    )
                )
                continue
            _validate_invoice_line(issues, line, quote_lines, index)
            item_id = line.get("quote_item_id")
            if isinstance(item_id, str):
                supplied.add(item_id)
        for item_id in quote_lines:
            if item_id not in supplied:
                issues.append(  # noqa: PERF401
                    DocumentIssue(
                        "quote_line_missing",
                        f"lines[{item_id}]",
                        "approved quote line is missing",
                    )
                )
    else:
        packages = snapshot.get("packages")
        if not isinstance(packages, list) or not packages:
            issues.append(
                DocumentIssue(
                    "missing_packages", "packages", "package rows are required"
                )
            )
            return issues
        quantities: dict[str, Decimal] = {}
        weight_units: set[str] = set()
        for index, package in enumerate(packages):
            if not isinstance(package, dict):
                issues.append(
                    DocumentIssue(
                        "invalid_package",
                        f"packages[{index}]",
                        "package must be an object",
                    )
                )
                continue
            _validate_package(issues, package, quote_lines, index)
            item_id = package.get("quote_item_id")
            quantity = _decimal(package.get("quantity"))
            if isinstance(item_id, str) and quantity is not None:
                quantities[item_id] = quantities.get(item_id, Decimal("0")) + quantity
            unit = package.get("weight_uom")
            if isinstance(unit, str) and unit.strip():
                weight_units.add(unit.strip().lower())
        if len(weight_units) > 1:
            issues.append(
                DocumentIssue(
                    "mixed_weight_uom",
                    "packages.weight_uom",
                    "all package weights must use one UOM",
                )
            )
        for item_id, quantity in quantities.items():
            expected = _decimal(quote_lines.get(item_id, {}).get("quantity"))
            if expected is not None and quantity > expected:
                issues.append(
                    DocumentIssue(
                        "quantity_exceeds_quote",
                        f"packages[{item_id}].quantity",
                        "packed quantity exceeds approved quote",
                    )
                )
    return issues


def _validate_invoice_line(
    issues: list[DocumentIssue],
    line: dict[str, Any],
    quote_lines: dict[str, dict[str, Any]],
    index: int,
) -> None:
    prefix = f"lines[{index}]"
    item_id = line.get("quote_item_id")
    expected = quote_lines.get(item_id) if isinstance(item_id, str) else None
    if expected is None:
        issues.append(
            DocumentIssue(
                "quote_line_not_found",
                f"{prefix}.quote_item_id",
                "line is not in approved quote",
            )
        )
        return
    expected_product = expected.get("product")
    if not isinstance(expected_product, dict):
        expected_product = {}
    if line.get("sku") != expected_product.get("sku"):
        issues.append(
            DocumentIssue(
                "sku_mismatch", f"{prefix}.sku", "SKU differs from approved quote"
            )
        )
    if line.get("description") != expected_product.get("name"):
        issues.append(
            DocumentIssue(
                "description_mismatch",
                f"{prefix}.description",
                "description differs from approved quote",
            )
        )
    if line.get("uom") != expected.get("unit"):
        issues.append(
            DocumentIssue(
                "uom_mismatch", f"{prefix}.uom", "UOM differs from approved quote"
            )
        )
    quantity = _decimal(line.get("quantity"))
    expected_quantity = _decimal(expected.get("quantity"))
    if quantity is None or expected_quantity is None or quantity > expected_quantity:
        issues.append(
            DocumentIssue(
                "quantity_exceeds_quote",
                f"{prefix}.quantity",
                "quantity exceeds approved quote",
            )
        )
    price = _decimal(line.get("unit_price"))
    expected_price = _decimal(expected.get("unit_price"))
    if price != expected_price:
        issues.append(
            DocumentIssue(
                "unit_price_mismatch",
                f"{prefix}.unit_price",
                "unit price differs from approved quote",
            )
        )
    amount = _decimal(line.get("amount"))
    if amount is None or price is None or quantity is None:
        issues.append(
            DocumentIssue(
                "amount_mismatch", f"{prefix}.amount", "amount cannot be computed"
            )
        )
    else:
        expected_amount = (price * quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if amount != expected_amount:
            issues.append(
                DocumentIssue(
                    "amount_mismatch",
                    f"{prefix}.amount",
                    "amount does not equal quantity times unit price",
                )
            )


def _validate_package(
    issues: list[DocumentIssue],
    package: dict[str, Any],
    quote_lines: dict[str, dict[str, Any]],
    index: int,
) -> None:
    prefix = f"packages[{index}]"
    item_id = package.get("quote_item_id")
    expected = quote_lines.get(item_id) if isinstance(item_id, str) else None
    if expected is None:
        issues.append(
            DocumentIssue(
                "quote_line_not_found",
                f"{prefix}.quote_item_id",
                "package is not in approved quote",
            )
        )
        return
    if package.get("uom") != expected.get("unit"):
        issues.append(
            DocumentIssue(
                "uom_mismatch", f"{prefix}.uom", "UOM differs from approved quote"
            )
        )
    for field in ("marks", "weight_uom"):
        if not str(package.get(field, "")).strip():
            issues.append(  # noqa: PERF401
                DocumentIssue(
                    "missing_field", f"{prefix}.{field}", f"{field} is required"
                )
            )
    net = _decimal(package.get("net_weight"))
    gross = _decimal(package.get("gross_weight"))
    if net is None or gross is None or gross < net:
        issues.append(
            DocumentIssue(
                "gross_less_than_net",
                f"{prefix}.gross_weight",
                "gross weight must be at least net weight",
            )
        )


def _validate_cross_document(  # noqa: C901
    commercial: dict[str, Any], packing: dict[str, Any]
) -> list[DocumentIssue]:
    issues: list[DocumentIssue] = []
    for field in (
        "buyer",
        "consignee",
        "document_reference",
        "incoterm",
        "named_place",
    ):
        if commercial.get(field) != packing.get(field):
            issues.append(  # noqa: PERF401
                DocumentIssue(
                    "cross_document_mismatch",
                    field,
                    f"{field} differs between documents",
                )
            )
    ci_quantities: dict[str, Decimal] = {}
    for line in commercial.get("lines", []):
        if isinstance(line, dict):
            item = line.get("quote_item_id")
            quantity = _decimal(line.get("quantity"))
            if isinstance(item, str) and quantity is not None:
                ci_quantities[item] = ci_quantities.get(item, Decimal("0")) + quantity
    pl_quantities: dict[str, Decimal] = {}
    for package in packing.get("packages", []):
        if isinstance(package, dict):
            item = package.get("quote_item_id")
            quantity = _decimal(package.get("quantity"))
            if isinstance(item, str) and quantity is not None:
                pl_quantities[item] = pl_quantities.get(item, Decimal("0")) + quantity
    for item in sorted(set(ci_quantities) | set(pl_quantities)):
        if ci_quantities.get(item, Decimal("0")) != pl_quantities.get(
            item, Decimal("0")
        ):
            issues.append(  # noqa: PERF401
                DocumentIssue(
                    "cross_document_quantity_mismatch",
                    f"quantity.{item}",
                    "invoice and packing quantities differ",
                )
            )
    return issues


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() else None


def _content_hash(document_type: str, snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"document_type": document_type, "snapshot": snapshot},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _get_idempotency(
    session: Session, *, org_id: str, actor_id: str, key: str
) -> IdempotencyRecord | None:
    return session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.org_id == org_id,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update()
    )


def _lock_idempotency_scope(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    idempotency_key: str,
) -> None:
    """Serialize first writers so a concurrent retry becomes a replay."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"trade-agent:documents:{org_id}:{actor_id}:{idempotency_key}"},
    )


def _check_idempotency(
    record: IdempotencyRecord, endpoint: str, request_hash: str
) -> None:
    if record.endpoint != endpoint or record.request_hash != request_hash:
        raise DocumentIdempotencyConflictError(
            "idempotency key was reused with a different request"
        )


def _set_result(
    record: DocumentSetRecord, *, idempotent_replay: bool
) -> DocumentSetResult:
    return DocumentSetResult(
        document_set_id=record.document_set_id,
        quotation_id=record.quotation_id,
        quote_revision_id=record.quote_revision_id,
        approved_content_hash=record.approved_content_hash,
        status=record.status,
        idempotent_replay=idempotent_replay,
    )


def _revision_result(
    record: DocumentSetRevisionRecord, *, idempotent_replay: bool
) -> DocumentRevisionResult:
    return DocumentRevisionResult(
        document_set_id=record.document_set_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        document_type=record.document_type,
        status=record.status,
        content_hash=record.content_hash,
        idempotent_replay=idempotent_replay,
    )
