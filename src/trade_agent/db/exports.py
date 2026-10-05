"""Deterministic private artifacts rendered from approved quote snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from typing import TYPE_CHECKING
import uuid

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from sqlalchemy import select

from trade_agent.contracts import AggregateType, QuoteRevision
from trade_agent.db.models import (
    IdempotencyRecord,
    QuoteApprovalRecord,
    QuoteArtifactRecord,
    QuoteRevisionRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

PROFORMA_INVOICE = "proforma_invoice"
PROFORMA_TEMPLATE_VERSION = "proforma-v1"
_SHA256_HEX_LENGTH = 64
_PDF_FONT = "STSong-Light"
_PDF_MARGIN = 42
_PDF_BOTTOM = 42
pdfmetrics.registerFont(UnicodeCIDFont(_PDF_FONT))


class ExportConflictError(Exception):
    """Raised when an export request is stale, mismatched, or already exists."""


class ExportNotApprovedError(ExportConflictError):
    """Raised when the requested revision has no approved decision."""


class ExportPendingError(ExportConflictError):
    """Raised when a prior artifact is still pending or failed."""


class ArtifactNotFoundError(Exception):
    """Raised when an artifact is not visible in the requested organization."""


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    artifact_id: str
    quotation_id: str
    revision_id: str
    artifact_type: str
    status: str
    content_hash: str
    amount_total: Decimal
    content_sha256: str | None
    idempotent_replay: bool


def create_quote_artifact(  # noqa: C901, PLR0912, PLR0913, PLR0915
    session: Session,
    *,
    org_id: str,
    quotation_id: str,
    revision_id: str,
    artifact_type: str,
    actor_id: str,
    idempotency_key: str,
    requested_content_hash: str | None = None,
    created_at: datetime | None = None,
) -> ArtifactResult:
    """Render one ready artifact from an approved immutable quote snapshot.

    The approval row is locked before inspecting existing artifacts, so two
    concurrent exports for one approved version cannot generate duplicate ready
    files. No external send or mutable quote data is involved.
    """
    _validate_inputs(
        org_id,
        quotation_id,
        revision_id,
        artifact_type,
        actor_id,
        idempotency_key,
        requested_content_hash,
    )
    now = created_at or datetime.now(UTC)
    request_hash = _request_hash({
        "artifact_type": artifact_type,
        "content_hash": requested_content_hash,
        "quotation_id": quotation_id,
        "revision_id": revision_id,
    })

    with session.begin_nested():
        existing_key = session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.org_id == org_id,
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing_key is not None:
            replay = _replay_or_conflict(
                session,
                existing_key,
                request_hash=request_hash,
                org_id=org_id,
                quotation_id=quotation_id,
                revision_id=revision_id,
                artifact_type=artifact_type,
            )
            if replay is not None:
                return replay

        approval = session.scalar(
            select(QuoteApprovalRecord)
            .where(
                QuoteApprovalRecord.org_id == org_id,
                QuoteApprovalRecord.quotation_id == quotation_id,
                QuoteApprovalRecord.revision_id == revision_id,
            )
            .with_for_update()
        )
        if approval is None or approval.decision != "approved":
            raise ExportNotApprovedError(
                "only an approved quote revision can be exported"
            )
        if (
            requested_content_hash is not None
            and requested_content_hash != approval.content_hash
        ):
            raise ExportConflictError("requested content hash does not match approval")

        existing_artifact = session.scalar(
            select(QuoteArtifactRecord)
            .where(
                QuoteArtifactRecord.org_id == org_id,
                QuoteArtifactRecord.quotation_id == quotation_id,
                QuoteArtifactRecord.revision_id == revision_id,
                QuoteArtifactRecord.artifact_type == artifact_type,
            )
            .with_for_update()
        )
        retry_artifact = None
        if existing_artifact is not None:
            if existing_artifact.status == "ready":
                return _artifact_result(existing_artifact, idempotent_replay=True)
            if existing_artifact.status != "failed":
                raise ExportPendingError(
                    "the artifact is not ready; retry after the prior attempt finishes"
                )
            retry_artifact = existing_artifact

        revision = session.scalar(
            select(QuoteRevisionRecord).where(
                QuoteRevisionRecord.org_id == org_id,
                QuoteRevisionRecord.quotation_id == quotation_id,
                QuoteRevisionRecord.revision_id == revision_id,
            )
        )
        if revision is None:
            raise ArtifactNotFoundError("quote revision was not found")
        if revision.content_hash != approval.content_hash:
            raise ExportConflictError("approval no longer matches the quote snapshot")

        quote = QuoteRevision.model_validate(revision.payload)
        if quote.content_hash != revision.content_hash:
            raise ExportConflictError("quote snapshot hash validation failed")
        if quote.content_hash != approval.content_hash:
            raise ExportConflictError("approval hash does not match quote snapshot")

        amount_total = sum(
            (item.line_amount for item in quote.items),
            start=Decimal("0.00"),
        )
        content = _render_proforma_invoice(quote, amount_total)
        artifact_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        content_digest = sha256(content).hexdigest()
        snapshot = quote.model_dump(mode="json")
        snapshot["content_hash"] = quote.content_hash
        snapshot["amount_total"] = format(amount_total, ".2f")
        if existing_key is None:
            session.add(
                IdempotencyRecord(
                    record_id=record_id,
                    org_id=org_id,
                    actor_id=actor_id,
                    endpoint="quotations.artifacts.create",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_aggregate_type=AggregateType.QUOTATION.value,
                    result_aggregate_id=quotation_id,
                    result_revision_id=revision_id,
                    created_at=now,
                )
            )
        if retry_artifact is None:
            session.add(
                QuoteArtifactRecord(
                    artifact_id=artifact_id,
                    org_id=org_id,
                    quotation_id=quotation_id,
                    revision_id=revision_id,
                    approval_id=approval.approval_id,
                    artifact_type=artifact_type,
                    template_version=PROFORMA_TEMPLATE_VERSION,
                    content_hash=quote.content_hash,
                    status="ready",
                    currency=quote.currency.value,
                    amount_total=amount_total,
                    snapshot=snapshot,
                    content_bytes=content,
                    content_sha256=content_digest,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            retry_artifact.status = "ready"
            retry_artifact.amount_total = amount_total
            retry_artifact.snapshot = snapshot
            retry_artifact.content_bytes = content
            retry_artifact.content_sha256 = content_digest
            retry_artifact.updated_at = now
        session.flush()
        return ArtifactResult(
            artifact_id=retry_artifact.artifact_id if retry_artifact else artifact_id,
            quotation_id=quotation_id,
            revision_id=revision_id,
            artifact_type=artifact_type,
            status="ready",
            content_hash=quote.content_hash,
            amount_total=amount_total,
            content_sha256=content_digest,
            idempotent_replay=existing_key is not None,
        )


def get_quote_artifact(
    session: Session,
    *,
    org_id: str,
    artifact_id: str,
) -> QuoteArtifactRecord:
    """Return a private artifact scoped to an organization."""
    artifact = session.scalar(
        select(QuoteArtifactRecord).where(
            QuoteArtifactRecord.org_id == org_id,
            QuoteArtifactRecord.artifact_id == artifact_id,
        )
    )
    if artifact is None:
        raise ArtifactNotFoundError("artifact was not found")
    if artifact.status != "ready" or artifact.content_bytes is None:
        raise ExportPendingError("artifact is not ready for download")
    return artifact


def _replay_or_conflict(  # noqa: PLR0913
    session: Session,
    existing: IdempotencyRecord,
    *,
    request_hash: str,
    org_id: str,
    quotation_id: str,
    revision_id: str,
    artifact_type: str,
) -> ArtifactResult | None:
    if existing.endpoint != "quotations.artifacts.create":
        raise ExportConflictError("idempotency key was used by another endpoint")
    if existing.request_hash != request_hash:
        raise ExportConflictError("idempotency key was reused with a different request")
    if (
        existing.result_aggregate_type != AggregateType.QUOTATION.value
        or existing.result_aggregate_id != quotation_id
        or existing.result_revision_id != revision_id
    ):
        raise ExportConflictError("idempotency key belongs to another quote revision")
    artifact = session.scalar(
        select(QuoteArtifactRecord).where(
            QuoteArtifactRecord.org_id == org_id,
            QuoteArtifactRecord.quotation_id == quotation_id,
            QuoteArtifactRecord.revision_id == revision_id,
            QuoteArtifactRecord.artifact_type == artifact_type,
        )
    )
    if artifact is None:
        raise RuntimeError("artifact idempotency record references a missing artifact")
    if artifact.status == "failed":
        return None
    if artifact.status != "ready":
        raise ExportPendingError("artifact is not ready; retry after the prior attempt")
    return _artifact_result(artifact, idempotent_replay=True)


def _artifact_result(
    artifact: QuoteArtifactRecord, *, idempotent_replay: bool
) -> ArtifactResult:
    return ArtifactResult(
        artifact_id=artifact.artifact_id,
        quotation_id=artifact.quotation_id,
        revision_id=artifact.revision_id,
        artifact_type=artifact.artifact_type,
        status=artifact.status,
        content_hash=artifact.content_hash,
        amount_total=artifact.amount_total,
        content_sha256=artifact.content_sha256,
        idempotent_replay=idempotent_replay,
    )


def _render_proforma_invoice(quote: QuoteRevision, amount_total: Decimal) -> bytes:
    """Render a stable PDF template; values come only from the quote snapshot."""
    lines = [
        "PRO FORMA INVOICE",
        f"Template: {PROFORMA_TEMPLATE_VERSION}",
        "Seller: Trade Agent Demo",
        "Customer: Not supplied in the approved snapshot",
        f"Quotation: {quote.quotation_id}",
        f"Revision: {quote.revision_id}",
        f"RFQ revision: {quote.rfq_revision_id}",
        f"Currency: {quote.currency.value}",
        f"Trade term: {quote.trade_term or '-'}",
        f"Named place: {quote.named_place or '-'}",
        "",
        "Items:",
    ]
    lines.extend(
        " | ".join((
            item.product.sku,
            item.product.name,
            f"{format(item.quantity.normalize(), 'f')} {item.unit.value}",
            f"{format(item.unit_price, '.4f')} {quote.currency.value}",
            f"{format(item.line_amount, '.2f')} {quote.currency.value}",
        ))
        for item in quote.items
    )
    lines.extend(("", f"TOTAL: {format(amount_total, '.2f')} {quote.currency.value}"))
    buffer = BytesIO()
    document = canvas.Canvas(
        buffer,
        pagesize=letter,
        pageCompression=0,
        invariant=1,
    )
    document.setTitle(f"Pro Forma Invoice {quote.quotation_id}")
    document.setAuthor("Trade Agent")
    document.setFont(_PDF_FONT, 10)
    y = 760
    for line in lines:
        if y < _PDF_BOTTOM:
            document.showPage()
            document.setFont(_PDF_FONT, 10)
            y = 760
        document.drawString(_PDF_MARGIN, y, line)
        y -= 14
    document.save()
    return buffer.getvalue()


def _validate_inputs(  # noqa: PLR0913, PLR0917
    org_id: str,
    quotation_id: str,
    revision_id: str,
    artifact_type: str,
    actor_id: str,
    idempotency_key: str,
    requested_content_hash: str | None,
) -> None:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (org_id, quotation_id, revision_id, actor_id, idempotency_key)
    ):
        raise ValueError("export identity fields must be non-empty")
    if artifact_type != PROFORMA_INVOICE:
        raise ValueError("unsupported artifact type")
    if requested_content_hash is not None and (
        len(requested_content_hash) != _SHA256_HEX_LENGTH
        or any(char not in "0123456789abcdef" for char in requested_content_hash)
    ):
        raise ValueError("requested_content_hash must be a lowercase SHA-256 digest")


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
