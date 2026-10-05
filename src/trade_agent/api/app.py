"""Run/Event HTTP API.

The identity headers are trusted upstream assertions, not credentials. Deployments
must place this app behind an authenticating gateway that overwrites these headers.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import os
import re
from threading import Lock
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from trade_agent.analytics import (
    AnalyticsMode,
    AnalyticsQueryEngine,
    create_analytics_router,
)
from trade_agent.contracts import (
    CanonicalUnit,
    CommercialInvoiceDocument,
    CommercialInvoiceLine,
    Currency,
    ExportDocumentType,
    InquiryAttachment,
    InquiryCase,
    InquiryCustomerRole,
    InquiryQueueState,
    InquiryReplyRevision,
    PackingListDocument,
    PackingListPackage,
    QualificationCheck,
    QualificationResult,
    ShipmentEvidence,
    ShipmentEvidenceStatus,
    ShipmentEvidenceType,
    ShipmentHandoff,
    ShipmentHandoffStatus,
    TranslationReviewDecision,
)
from trade_agent.db.approvals import (
    ApprovalConflictError,
    ApprovalHashMismatchError,
    ApprovalNotFoundError,
    ApprovalResult,
    decide_quote_revision,
)
from trade_agent.db.documents import (
    DocumentConflictError,
    DocumentHashMismatchError,
    DocumentIdempotencyConflictError,
    DocumentNotApprovedError,
    DocumentNotFoundError,
    DocumentRevisionResult,
    DocumentSetResult,
    DocumentValidationError,
    DocumentValidationResult,
    append_document_revision,
    create_document_set,
    get_document_set,
    issue_document_set,
    ready_document_set,
    validate_document_set,
    void_document_set,
)
from trade_agent.db.exports import (
    PROFORMA_INVOICE,
    ArtifactNotFoundError,
    ArtifactResult,
    ExportConflictError,
    ExportNotApprovedError,
    ExportPendingError,
    create_quote_artifact,
    get_quote_artifact,
)
from trade_agent.db.inquiries import (
    InquiryAuthorizationError,
    InquiryConflictError,
    InquiryHashMismatchError,
    InquiryIdempotencyConflictError,
    InquiryNotFoundError,
    InquiryQueueItem,
    InquirySelfReviewError,
    TranslationNotRequiredError,
    append_inquiry_reply_revision,
    create_inquiry_case,
    effective_inquiry_queue_state,
    get_current_reply,
    get_inquiry_case,
    get_translation_review,
    list_inquiry_queue,
    review_inquiry_translation,
)
from trade_agent.db.models import (
    DocumentSetRevisionRecord,
    InquiryCaseRecord,
    QualificationCheckRecord,
    RFQRevisionRecord,
    RunEventRecord,
    RunRecord,
    ShipmentHandoffEvidenceRecord,
    ShipmentHandoffGateDecisionRecord,
    ShipmentHandoffRecord,
)
from trade_agent.db.qualification import (
    QualificationAlreadyDecidedError,
    QualificationAuthorizationError,
    QualificationConflictError,
    QualificationDecisionResult,
    QualificationGateError,
    QualificationHashMismatchError,
    QualificationIdempotencyConflictError,
    QualificationNotFoundError,
    QualificationView,
    QualificationWriteResult,
    create_qualification_check,
    decide_qualification_check,
    get_qualification_check,
    list_qualification_checks,
)
from trade_agent.db.runs import (
    InvalidRunTransitionError,
    RunConflictError,
    RunNotFoundError,
    RunResult,
    create_run,
    get_run,
    resume_run,
)
from trade_agent.db.shipments import (
    ShipmentAuthorizationError,
    ShipmentConflictError,
    ShipmentEvidenceResult,
    ShipmentGateError,
    ShipmentGateResult,
    ShipmentHandoffResult,
    ShipmentHashMismatchError,
    ShipmentIdempotencyConflictError,
    ShipmentNotApprovedError,
    ShipmentNotFoundError,
    ShipmentStatusTransitionError,
    ShipmentTransitionResult,
    append_shipment_evidence,
    create_shipment_handoff,
    evaluate_shipment_gate,
    get_shipment_handoff,
    list_shipment_evidence,
    list_shipment_gate_decisions,
    transition_shipment_handoff,
)

SessionFactory = sessionmaker[Session]
Role = Literal["sales", "reviewer", "admin"]
_MAX_IDENTITY_LENGTH = 128


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    rfq_revision_id: str = Field(min_length=1, max_length=128)
    graph_version: str = Field(min_length=1, max_length=64)


class RunView(BaseModel):
    run_id: str
    status: str
    rfq_id: str
    rfq_revision_id: str
    graph_version: str
    thread_id: str
    wait_reason: str | None
    error_code: str | None
    event_seq: int
    created_at: datetime
    updated_at: datetime


class CreateRunResponse(BaseModel):
    run_id: str
    status: str
    event_seq: int
    idempotent_replay: bool


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_key: str | None = Field(default=None, min_length=1, max_length=255)


class RunEventView(BaseModel):
    event_id: str
    event_seq: int
    event_type: str
    node_name: str | None
    occurred_at: datetime


class RunEventPage(BaseModel):
    run_id: str
    page: int
    page_size: int
    total: int
    events: list[RunEventView]


class QuoteApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)


class QuoteApprovalResponse(BaseModel):
    approval_id: str
    quotation_id: str
    revision_id: str
    content_hash: str
    decision: Literal["approved", "rejected"]
    idempotent_replay: bool


class QuoteArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    artifact_type: Literal["proforma_invoice"] = PROFORMA_INVOICE
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class QuoteArtifactResponse(BaseModel):
    artifact_id: str
    quotation_id: str
    revision_id: str
    artifact_type: str
    status: str
    content_hash: str
    amount_total: str
    content_sha256: str | None
    idempotent_replay: bool


class DocumentSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_set_id: str = Field(min_length=1, max_length=128)
    quotation_id: str = Field(min_length=1, max_length=128)
    quote_revision_id: str = Field(min_length=1, max_length=128)
    approved_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CommercialInvoiceLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quote_item_id: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=4000)
    quantity: Decimal
    uom: CanonicalUnit
    unit_price: Decimal
    amount: Decimal


class PackingListPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    package_id: str = Field(min_length=1, max_length=128)
    quote_item_id: str = Field(min_length=1, max_length=128)
    marks: str = Field(min_length=1, max_length=1000)
    quantity: Decimal
    uom: CanonicalUnit
    net_weight: Decimal
    gross_weight: Decimal
    weight_uom: str = Field(min_length=1, max_length=16)


class DocumentRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_type: Literal["commercial_invoice", "packing_list"]
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
    currency: Currency | None = None
    incoterm: str = Field(min_length=1, max_length=32)
    named_place: str | None = Field(default=None, max_length=128)
    lines: list[CommercialInvoiceLineRequest] = Field(
        default_factory=list, max_length=100
    )
    packages: list[PackingListPackageRequest] = Field(
        default_factory=list, max_length=500
    )
    expected_revision_id: str | None = Field(default=None, max_length=128)
    status: Literal["draft", "ready", "issued", "void"] = "draft"


class DocumentSetResponse(BaseModel):
    document_set_id: str
    quotation_id: str
    quote_revision_id: str
    approved_content_hash: str
    status: Literal["draft", "ready", "issued", "void"]
    idempotent_replay: bool


class DocumentRevisionResponse(BaseModel):
    document_set_id: str
    revision_id: str
    revision_no: int
    document_type: Literal["commercial_invoice", "packing_list"]
    status: Literal["draft", "ready", "issued", "void"]
    content_hash: str
    idempotent_replay: bool


class DocumentIssueView(BaseModel):
    code: str
    field: str
    message: str


class DocumentValidationResponse(BaseModel):
    document_set_id: str
    valid: bool
    ready: bool
    issues: list[DocumentIssueView]


class DocumentSetDetailResponse(DocumentSetResponse):
    revisions: list[DocumentRevisionResponse]


class ShipmentHandoffCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    shipment_handoff_id: str = Field(min_length=1, max_length=128)
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
    required_documents: list[str] = Field(default_factory=list, max_length=100)


class ShipmentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_type: ShipmentEvidenceType
    check_key: str | None = Field(default=None, max_length=128)
    evidence_ref: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ShipmentEvidenceStatus = ShipmentEvidenceStatus.VERIFIED
    notes: str | None = Field(default=None, max_length=4000)
    expires_at: datetime | None = None
    parent_evidence_id: str | None = Field(default=None, max_length=128)
    expected_evidence_id: str | None = Field(default=None, max_length=128)


class ShipmentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_status: ShipmentHandoffStatus
    reason: str | None = Field(default=None, max_length=4000)
    override: bool = False
    booking_reference: str | None = Field(default=None, max_length=255)
    eta: datetime | None = None


class ShipmentEvidenceView(BaseModel):
    evidence_id: str
    revision_no: int
    evidence_type: str
    check_key: str | None
    evidence_ref: str
    content_hash: str
    status: str
    notes: str | None
    expires_at: datetime | None
    parent_evidence_id: str | None
    created_at: datetime
    actor_id: str


class ShipmentGateDecisionView(BaseModel):
    decision_id: str
    target_status: str
    decision: str
    gate_hash: str
    overridden: bool
    reason: str | None
    actor_id: str
    actor_role: str
    decided_at: datetime


class ShipmentHandoffResponse(BaseModel):
    shipment_handoff_id: str
    quotation_id: str
    quote_revision_id: str
    approved_content_hash: str
    document_set_id: str | None
    selected_incoterm: str
    named_place: str
    responsibility_split: dict[str, str]
    freight_forwarder_name: str | None
    freight_forwarder_quote_ref: str | None
    carrier_name: str | None
    insurance_scope: str | None
    insurance_expires_at: datetime | None
    required_documents: list[str]
    booking_reference: str | None
    eta: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
    created_by: str
    idempotent_replay: bool = False
    evidence: list[ShipmentEvidenceView] = Field(default_factory=list)
    gate_decisions: list[ShipmentGateDecisionView] = Field(default_factory=list)


class ShipmentEvidenceResponse(BaseModel):
    shipment_handoff_id: str
    evidence_id: str
    revision_no: int
    evidence_type: str
    check_key: str | None
    status: str
    content_hash: str
    idempotent_replay: bool


class ShipmentGateResponse(BaseModel):
    shipment_handoff_id: str
    valid: bool
    gate_hash: str
    issues: list[str]


class ShipmentTransitionResponse(BaseModel):
    shipment_handoff_id: str
    status: str
    target_status: str
    decision_id: str | None
    gate_hash: str
    idempotent_replay: bool


class InquiryQueueItemView(BaseModel):
    inquiry_id: str
    rfq_id: str
    source_channel: str
    customer_role: str
    original_language: str
    received_at: datetime
    response_due_at: datetime
    owner_id: str | None
    queue_state: str
    is_overdue: bool
    current_reply_revision_id: str | None
    current_reply_revision_no: int | None


class InquiryQueueResponse(BaseModel):
    items: list[InquiryQueueItemView]
    limit: int
    offset: int


class InquiryCaseView(InquiryQueueItemView):
    created_at: datetime
    created_by: str
    attachments: list[InquiryAttachment]


class InquiryReplyView(BaseModel):
    reply_revision_id: str
    revision_no: int
    rfq_revision_id: str
    source_language: str
    target_language: str
    source_content: str
    translated_content: str | None
    template_version: str
    attachments: list[InquiryAttachment]
    content_hash: str
    created_at: datetime
    created_by: str


class InquiryTranslationReviewView(BaseModel):
    review_id: str
    content_hash: str
    decision: Literal["approved", "rejected"]
    actor_id: str
    reason: str | None
    reviewed_at: datetime


class InquiryDetailView(BaseModel):
    case: InquiryCaseView
    current_reply: InquiryReplyView | None
    translation_review: InquiryTranslationReviewView | None
    translation_review_state: Literal["not_required", "pending", "approved", "rejected"]


class InquiryReplyRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rfq_revision_id: str = Field(min_length=1, max_length=128)
    source_language: str = Field(min_length=2, max_length=16)
    target_language: str = Field(min_length=2, max_length=16)
    source_content: str = Field(min_length=1, max_length=20000)
    translated_content: str | None = Field(default=None, max_length=20000)
    template_version: str = Field(default="reply-v1", min_length=1, max_length=64)
    attachments: list[InquiryAttachment] = Field(default_factory=list, max_length=20)
    expected_content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class InquiryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    inquiry_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    source_channel: str = Field(min_length=1, max_length=64)
    customer_role: Literal[
        "end_customer", "representative", "distributor", "unknown"
    ] = "unknown"
    original_language: str = Field(min_length=2, max_length=16)
    received_at: datetime
    response_due_at: datetime
    owner_id: str | None = Field(default=None, max_length=128)
    attachments: list[InquiryAttachment] = Field(default_factory=list, max_length=20)


class InquiryTranslationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)


class InquiryReplyRevisionResponse(BaseModel):
    inquiry_id: str
    reply_revision_id: str
    revision_no: int
    content_hash: str
    idempotent_replay: bool


class InquiryCreateResponse(BaseModel):
    inquiry_id: str
    idempotent_replay: bool


class InquiryTranslationReviewResponse(BaseModel):
    review_id: str
    inquiry_id: str
    reply_revision_id: str
    content_hash: str
    decision: Literal["approved", "rejected"]
    idempotent_replay: bool


class QualificationCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str | None = Field(default=None, max_length=128)
    inquiry_id: str | None = Field(default=None, max_length=128)
    source: str = Field(min_length=1, max_length=255)
    reference: str = Field(min_length=1, max_length=1024)
    checked_at: datetime
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_attachment_ref: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=4000)


class QualificationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    notes: str | None = Field(default=None, max_length=4000)


class QualificationCheckView(BaseModel):
    check_id: str
    org_id: str
    customer_id: str | None
    inquiry_id: str | None
    source: str
    reference: str
    checked_at: datetime
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    evidence_hash: str
    evidence_attachment_ref: str | None
    notes: str | None
    reviewer_id: str | None
    created_at: datetime
    created_by: str


class QualificationDecisionView(BaseModel):
    decision_id: str
    check_id: str
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    evidence_hash: str
    reviewer_id: str
    notes: str | None
    decided_at: datetime


class QualificationListItemView(QualificationCheckView):
    decision: QualificationDecisionView | None
    effective_result: Literal["clear", "potential_match", "blocked", "unverified"]


class QualificationListResponse(BaseModel):
    items: list[QualificationListItemView]
    limit: int
    offset: int


class QualificationDetailView(BaseModel):
    check: QualificationCheckView
    decision: QualificationDecisionView | None
    effective_result: Literal["clear", "potential_match", "blocked", "unverified"]


class QualificationCheckResponse(BaseModel):
    check_id: str
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    evidence_hash: str
    idempotent_replay: bool


class QualificationDecisionResponse(BaseModel):
    decision_id: str
    check_id: str
    result: Literal["clear", "potential_match", "blocked", "unverified"]
    evidence_hash: str
    reviewer_id: str
    idempotent_replay: bool


class _ApiProblemError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


class _Identity(BaseModel):
    org_id: str
    actor_id: str
    role: Role


def create_app(  # noqa: C901, PLR0915 - app composition keeps the public API in one module
    session_factory: SessionFactory | None = None,
    *,
    engine: Engine | None = None,
) -> FastAPI:
    """Build the HTTP API with an injected SQLAlchemy session factory or engine."""
    if session_factory is not None and engine is not None:
        raise ValueError("pass either session_factory or engine, not both")

    owns_engine = False
    if engine is not None:
        session_factory = sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:  # noqa: RUF029
        yield
        if application.state.owns_engine and application.state.engine is not None:
            application.state.engine.dispose()

    application = FastAPI(
        title="Trade Agent Run API",
        version="1.0.0",
        description=(
            "Run/Event API. X-Org-Id, X-Actor-Id, and X-Role are trusted upstream "
            "assertions for a local service boundary; they do not authenticate callers."
        ),
        lifespan=lifespan,
    )
    application.state.session_factory = session_factory
    application.state.engine = engine
    application.state.owns_engine = owns_engine
    application.state.engine_lock = Lock()
    analytics_mode = os.environ.get("TRADE_ANALYTICS_MODE", AnalyticsMode.FAKE.value)
    application.include_router(
        create_analytics_router(
            AnalyticsQueryEngine(
                session_factory=session_factory,
                mode=analytics_mode,
            )
        )
    )

    @application.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id_header = request.headers.get("X-Request-Id", "")
        try:
            request_id = str(UUID(request_id_header))
        except (ValueError, AttributeError):
            request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @application.exception_handler(_ApiProblemError)
    def api_problem_handler(request: Request, exc: _ApiProblemError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @application.exception_handler(RequestValidationError)
    def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request, 422, "invalid_request", "Request validation failed."
        )

    @application.exception_handler(StarletteHTTPException)
    def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code, message = {
            404: ("not_found", "The requested resource was not found."),
            405: ("method_not_allowed", "The HTTP method is not allowed."),
        }.get(exc.status_code, ("http_error", "The request could not be completed."))
        return _error_response(request, exc.status_code, code, message)

    @application.exception_handler(RunNotFoundError)
    def run_not_found_handler(request: Request, exc: RunNotFoundError) -> JSONResponse:
        del exc
        return _error_response(
            request, 404, "not_found", "The requested run was not found."
        )

    @application.exception_handler(InquiryNotFoundError)
    def inquiry_not_found_handler(
        request: Request, exc: InquiryNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request, 404, "inquiry_not_found", "The inquiry was not found."
        )

    @application.exception_handler(InquiryAuthorizationError)
    def inquiry_authorization_handler(
        request: Request, exc: InquiryAuthorizationError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            403,
            "forbidden",
            "Only reviewers and admins can review translations.",
        )

    @application.exception_handler(InquiryConflictError)
    def inquiry_conflict_handler(
        request: Request, exc: InquiryConflictError
    ) -> JSONResponse:
        if isinstance(exc, InquiryHashMismatchError):
            code, message = (
                "inquiry_hash_mismatch",
                "The reply content no longer matches the displayed snapshot.",
            )
        elif isinstance(exc, InquiryIdempotencyConflictError):
            code, message = (
                "idempotency_conflict",
                "The idempotency key was reused with a different request.",
            )
        elif isinstance(exc, InquirySelfReviewError):
            code, message = (
                "self_review_forbidden",
                "Reply authors cannot review their own translation.",
            )
        elif isinstance(exc, TranslationNotRequiredError):
            code, message = (
                "translation_not_required",
                "A same-language reply does not require translation review.",
            )
        else:
            code, message = (
                "inquiry_conflict",
                "The inquiry operation conflicts with current data.",
            )
        return _error_response(request, 409, code, message)

    @application.exception_handler(QualificationNotFoundError)
    def qualification_not_found_handler(
        request: Request, exc: QualificationNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            404,
            "qualification_not_found",
            "The qualification check was not found.",
        )

    @application.exception_handler(QualificationAuthorizationError)
    def qualification_authorization_handler(
        request: Request, exc: QualificationAuthorizationError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            403,
            "forbidden",
            "Only reviewers and admins can confirm qualification results.",
        )

    @application.exception_handler(QualificationConflictError)
    def qualification_conflict_handler(
        request: Request, exc: QualificationConflictError
    ) -> JSONResponse:
        if isinstance(exc, QualificationGateError):
            code, message = (
                "qualification_gate_blocked",
                str(exc),
            )
        elif isinstance(exc, QualificationHashMismatchError):
            code, message = (
                "qualification_hash_mismatch",
                "The screening evidence no longer matches the displayed snapshot.",
            )
        elif isinstance(exc, QualificationIdempotencyConflictError):
            code, message = (
                "idempotency_conflict",
                "The idempotency key was reused with a different request.",
            )
        elif isinstance(exc, QualificationAlreadyDecidedError):
            code, message = (
                "qualification_already_decided",
                "The qualification check already has a final decision.",
            )
        else:
            code, message = (
                "qualification_conflict",
                "The qualification operation conflicts with current data.",
            )
        return _error_response(request, 409, code, message)

    @application.exception_handler(RunConflictError)
    def run_conflict_handler(request: Request, exc: RunConflictError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            409,
            "conflict",
            "The run or event key conflicts with existing data.",
        )

    @application.exception_handler(InvalidRunTransitionError)
    def transition_error_handler(
        request: Request, exc: InvalidRunTransitionError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            409,
            "invalid_transition",
            "The run is not in a state that allows this operation.",
        )

    @application.exception_handler(ApprovalNotFoundError)
    def approval_not_found_handler(
        request: Request, exc: ApprovalNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request, 404, "quote_not_found", "The quote revision was not found."
        )

    @application.exception_handler(ApprovalConflictError)
    def approval_conflict_handler(
        request: Request, exc: ApprovalConflictError
    ) -> JSONResponse:
        if isinstance(exc, ApprovalHashMismatchError):
            return _error_response(
                request,
                409,
                "quote_hash_mismatch",
                "The displayed quote no longer matches the stored snapshot.",
            )
        return _error_response(
            request,
            409,
            "quote_conflict",
            "The quote revision is stale or already has a final decision.",
        )

    @application.exception_handler(ArtifactNotFoundError)
    def artifact_not_found_handler(
        request: Request, exc: ArtifactNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request, 404, "artifact_not_found", "The artifact was not found."
        )

    @application.exception_handler(ExportConflictError)
    def export_conflict_handler(
        request: Request, exc: ExportConflictError
    ) -> JSONResponse:
        if isinstance(exc, ExportNotApprovedError):
            code, message = (
                "quote_not_approved",
                "Only an approved quote revision can be exported.",
            )
        elif isinstance(exc, ExportPendingError):
            code, message = (
                "artifact_pending",
                "The artifact is not ready for download; retry later.",
            )
        else:
            code, message = (
                "export_conflict",
                "The export request does not match the approved quote snapshot.",
            )
        return _error_response(request, 409, code, message)

    @application.exception_handler(DocumentNotFoundError)
    def document_not_found_handler(
        request: Request, exc: DocumentNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request, 404, "document_set_not_found", "The document set was not found."
        )

    @application.exception_handler(DocumentConflictError)
    def document_conflict_handler(
        request: Request, exc: DocumentConflictError
    ) -> JSONResponse:
        if isinstance(exc, DocumentHashMismatchError):
            code, message = (
                "document_quote_hash_mismatch",
                "The document set is not bound to the exact approved quote snapshot.",
            )
        elif isinstance(exc, DocumentNotApprovedError):
            code, message = (
                "quote_not_approved",
                "Only an approved quote revision can anchor export documents.",
            )
        elif isinstance(exc, DocumentIdempotencyConflictError):
            code, message = (
                "idempotency_conflict",
                "The idempotency key was reused with a different request.",
            )
        elif isinstance(exc, DocumentValidationError):
            code, message = (
                "document_validation_blocked",
                "The document set has deterministic consistency blockers.",
            )
        else:
            code, message = (
                "document_conflict",
                "The document operation conflicts with current data.",
            )
        return _error_response(request, 409, code, message)

    @application.exception_handler(ShipmentNotFoundError)
    def shipment_not_found_handler(
        request: Request, exc: ShipmentNotFoundError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            404,
            "shipment_handoff_not_found",
            "The shipment handoff was not found.",
        )

    @application.exception_handler(ShipmentAuthorizationError)
    def shipment_authorization_handler(
        request: Request, exc: ShipmentAuthorizationError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            403,
            "forbidden",
            "Only reviewers and admins can approve shipment gate transitions.",
        )

    @application.exception_handler(ShipmentConflictError)
    def shipment_conflict_handler(
        request: Request, exc: ShipmentConflictError
    ) -> JSONResponse:
        if isinstance(exc, ShipmentGateError):
            code, message = "shipment_gate_blocked", "; ".join(exc.issues)
        elif isinstance(exc, ShipmentHashMismatchError):
            code, message = (
                "shipment_quote_hash_mismatch",
                "The shipment handoff is not bound to the exact approved quote snapshot.",
            )
        elif isinstance(exc, ShipmentNotApprovedError):
            code, message = (
                "quote_not_approved",
                "Only an approved quote revision can anchor a shipment handoff.",
            )
        elif isinstance(exc, ShipmentIdempotencyConflictError):
            code, message = (
                "idempotency_conflict",
                "The idempotency key was reused with a different request.",
            )
        elif isinstance(exc, ShipmentStatusTransitionError):
            code, message = "shipment_invalid_transition", str(exc)
        else:
            code, message = "shipment_conflict", str(exc)
        return _error_response(request, 409, code, message)

    @application.exception_handler(ValueError)
    def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        del exc
        return _error_response(
            request, 422, "invalid_request", "Invalid request value."
        )

    @application.exception_handler(IntegrityError)
    def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        del exc
        return _error_response(
            request, 409, "conflict", "The request conflicts with existing data."
        )

    @application.exception_handler(Exception)
    def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error_response(
            request, 500, "internal_error", "The request could not be completed."
        )

    def require_identity(
        x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_role: Annotated[str | None, Header(alias="X-Role")] = None,
    ) -> _Identity:
        if not x_org_id or not x_actor_id or not x_role:
            raise _ApiProblemError(
                401,
                "identity_required",
                "X-Org-Id, X-Actor-Id, and X-Role are required.",
            )
        org_id = x_org_id.strip()
        actor_id = x_actor_id.strip()
        if (
            not org_id
            or len(org_id) > _MAX_IDENTITY_LENGTH
            or not actor_id
            or len(actor_id) > _MAX_IDENTITY_LENGTH
        ):
            raise _ApiProblemError(
                401, "identity_invalid", "The caller identity is invalid."
            )
        if x_role not in {"sales", "reviewer", "admin"}:
            raise _ApiProblemError(403, "forbidden", "The caller role is not allowed.")
        return _Identity(org_id=org_id, actor_id=actor_id, role=x_role)  # type: ignore[arg-type]

    def get_session_factory(request: Request) -> SessionFactory:
        factory = request.app.state.session_factory
        if factory is not None:
            return factory
        with request.app.state.engine_lock:
            factory = request.app.state.session_factory
            if factory is None:
                database_url = os.environ.get("DATABASE_URL")
                if not database_url:
                    raise _ApiProblemError(
                        503,
                        "database_not_configured",
                        "DATABASE_URL or an injected SQLAlchemy engine is required.",
                    )
                created_engine = create_engine(database_url, pool_pre_ping=True)
                factory = sessionmaker(created_engine, expire_on_commit=False)
                request.app.state.engine = created_engine
                request.app.state.owns_engine = True
                request.app.state.session_factory = factory
        return factory

    def authorized_run(session: Session, run_id: str, identity: _Identity) -> RunRecord:
        record = get_run(session, run_id)
        if record.org_id != identity.org_id:
            raise RunNotFoundError(run_id)
        return record

    def authorized_revision(
        session: Session, body: CreateRunRequest, identity: _Identity
    ) -> None:
        revision = session.scalar(
            select(RFQRevisionRecord).where(
                RFQRevisionRecord.revision_id == body.rfq_revision_id,
                RFQRevisionRecord.rfq_id == body.rfq_id,
                RFQRevisionRecord.org_id == identity.org_id,
            )
        )
        if revision is None:
            raise _ApiProblemError(
                404,
                "rfq_revision_not_found",
                "The RFQ revision was not found for this organization.",
            )

    @application.get(
        "/api/v1/qualification-checks",
        response_model=QualificationListResponse,
    )
    def qualification_check_list_route(  # noqa: PLR0913, PLR0917
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        customer_id: Annotated[str | None, Query(max_length=128)] = None,
        inquiry_id: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> QualificationListResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            items = list_qualification_checks(
                session,
                org_id=identity.org_id,
                customer_id=customer_id.strip() if customer_id else None,
                inquiry_id=inquiry_id.strip() if inquiry_id else None,
                limit=limit,
                offset=offset,
            )
        return QualificationListResponse(
            items=[_qualification_list_item_view(item) for item in items],
            limit=limit,
            offset=offset,
        )

    @application.get(
        "/api/v1/qualification-checks/{check_id}",
        response_model=QualificationDetailView,
    )
    def qualification_check_detail_route(
        check_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> QualificationDetailView:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            item = get_qualification_check(
                session, org_id=identity.org_id, check_id=check_id
            )
        if item is None:
            raise QualificationNotFoundError("qualification check was not found")
        return _qualification_detail_view(item)

    @application.post(
        "/api/v1/qualification-checks",
        response_model=QualificationCheckResponse,
    )
    def create_qualification_check_route(
        body: QualificationCheckRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> QualificationCheckResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        now = datetime.now(UTC)
        check = QualificationCheck(
            org_id=identity.org_id,
            check_id=str(uuid4()),
            customer_id=body.customer_id,
            inquiry_id=body.inquiry_id,
            source=body.source,
            reference=body.reference,
            checked_at=body.checked_at,
            result=QualificationResult(body.result),
            evidence_hash=body.evidence_hash,
            evidence_attachment_ref=body.evidence_attachment_ref,
            notes=body.notes,
            created_at=now,
            created_by=identity.actor_id,
        )
        with factory() as session, session.begin():
            result = create_qualification_check(
                session,
                check,
                actor_role=identity.role,
                idempotency_key=idempotency_key.strip(),
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _qualification_check_response(result)

    @application.post(
        "/api/v1/qualification-checks/{check_id}/decision",
        response_model=QualificationDecisionResponse,
    )
    def decide_qualification_check_route(  # noqa: PLR0913, PLR0917
        check_id: str,
        body: QualificationDecisionRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> QualificationDecisionResponse:
        _require_role(identity, {"reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        with factory() as session, session.begin():
            result = decide_qualification_check(
                session,
                org_id=identity.org_id,
                check_id=check_id,
                evidence_hash=body.evidence_hash,
                result=QualificationResult(body.result),
                actor_id=identity.actor_id,
                actor_role=identity.role,
                idempotency_key=idempotency_key.strip(),
                notes=body.notes,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _qualification_decision_response(result)

    @application.post(
        "/api/v1/inquiries",
        response_model=InquiryCreateResponse,
    )
    def create_inquiry_route(
        body: InquiryCreateRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> InquiryCreateResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        now = datetime.now(UTC)
        case = InquiryCase(
            org_id=identity.org_id,
            inquiry_id=body.inquiry_id,
            rfq_id=body.rfq_id,
            source_channel=body.source_channel,
            customer_role=InquiryCustomerRole(body.customer_role),
            original_language=body.original_language,
            received_at=body.received_at,
            response_due_at=body.response_due_at,
            owner_id=body.owner_id,
            queue_state=InquiryQueueState.OPEN,
            attachments=tuple(body.attachments),
            created_at=now,
            created_by=identity.actor_id,
        )
        with factory() as session, session.begin():
            result = create_inquiry_case(
                session,
                case,
                idempotency_key=idempotency_key.strip(),
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return InquiryCreateResponse(
            inquiry_id=result.inquiry_id,
            idempotent_replay=result.idempotent_replay,
        )

    @application.get("/api/v1/inquiries/queue", response_model=InquiryQueueResponse)
    def inquiry_queue_route(
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        include_closed: Annotated[bool, Query()] = False,  # noqa: FBT002
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> InquiryQueueResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        now = datetime.now(UTC)
        with factory() as session:
            items = list_inquiry_queue(
                session,
                org_id=identity.org_id,
                include_closed=include_closed,
                limit=limit,
                offset=offset,
                now=now,
            )
        return InquiryQueueResponse(
            items=[_inquiry_queue_item_view(item) for item in items],
            limit=limit,
            offset=offset,
        )

    @application.get("/api/v1/inquiries/{inquiry_id}", response_model=InquiryDetailView)
    def get_inquiry_route(
        inquiry_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> InquiryDetailView:
        _require_role(identity, {"sales", "reviewer", "admin"})
        now = datetime.now(UTC)
        with factory() as session:
            case = get_inquiry_case(
                session, org_id=identity.org_id, inquiry_id=inquiry_id
            )
            if case is None:
                raise InquiryNotFoundError("inquiry was not found")
            current_reply = get_current_reply(
                session, org_id=identity.org_id, inquiry_id=inquiry_id
            )
            review = (
                get_translation_review(
                    session,
                    org_id=identity.org_id,
                    inquiry_id=inquiry_id,
                    reply_revision_id=current_reply.reply_revision_id,
                )
                if current_reply is not None
                else None
            )
            case_state, is_overdue = effective_inquiry_queue_state(case, now=now)
            case_view = _inquiry_case_view(case, case_state, is_overdue)
            reply_view = (
                InquiryReplyView(
                    reply_revision_id=current_reply.reply_revision_id,
                    revision_no=current_reply.revision_no,
                    rfq_revision_id=current_reply.rfq_revision_id,
                    source_language=current_reply.source_language,
                    target_language=current_reply.target_language,
                    source_content=current_reply.source_content,
                    translated_content=current_reply.translated_content,
                    template_version=current_reply.template_version,
                    attachments=current_reply.attachments,
                    content_hash=current_reply.content_hash,
                    created_at=current_reply.created_at,
                    created_by=current_reply.created_by,
                )
                if current_reply is not None
                else None
            )
            review_view = (
                InquiryTranslationReviewView(
                    review_id=review.review_id,
                    content_hash=review.content_hash,
                    decision=review.decision,
                    actor_id=review.actor_id,
                    reason=review.reason,
                    reviewed_at=review.reviewed_at,
                )
                if review is not None
                else None
            )
        if current_reply is None or (
            current_reply.source_language.lower()
            == current_reply.target_language.lower()
        ):
            review_state = "not_required"
        elif review is None:
            review_state = "pending"
        else:
            review_state = review.decision
        return InquiryDetailView(
            case=case_view,
            current_reply=reply_view,
            translation_review=review_view,
            translation_review_state=review_state,
        )

    @application.post(
        "/api/v1/inquiries/{inquiry_id}/reply-revisions",
        response_model=InquiryReplyRevisionResponse,
        status_code=201,
    )
    def create_inquiry_reply_route(  # noqa: PLR0913, PLR0917
        inquiry_id: str,
        body: InquiryReplyRevisionRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> InquiryReplyRevisionResponse:
        _require_role(identity, {"sales"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        now = datetime.now(UTC)
        with factory() as session, session.begin():
            case = get_inquiry_case(
                session, org_id=identity.org_id, inquiry_id=inquiry_id
            )
            if case is None:
                raise InquiryNotFoundError("inquiry was not found")
            current = get_current_reply(
                session, org_id=identity.org_id, inquiry_id=inquiry_id
            )
            revision = InquiryReplyRevision(
                org_id=identity.org_id,
                inquiry_id=inquiry_id,
                rfq_id=case.rfq_id,
                reply_revision_id=str(uuid4()),
                revision_no=(current.revision_no if current is not None else 0) + 1,
                parent_reply_revision_id=(
                    current.reply_revision_id if current is not None else None
                ),
                rfq_revision_id=body.rfq_revision_id,
                source_language=body.source_language,
                target_language=body.target_language,
                source_content=body.source_content,
                translated_content=body.translated_content,
                template_version=body.template_version,
                attachments=tuple(body.attachments),
                created_at=now,
                created_by=identity.actor_id,
            )
            result = append_inquiry_reply_revision(
                session,
                revision,
                expected_content_hash=body.expected_content_hash,
                idempotency_key=idempotency_key.strip(),
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return InquiryReplyRevisionResponse(
            inquiry_id=result.inquiry_id,
            reply_revision_id=result.reply_revision_id,
            revision_no=result.revision_no,
            content_hash=result.content_hash,
            idempotent_replay=result.idempotent_replay,
        )

    @application.post(
        "/api/v1/inquiries/{inquiry_id}/reply-revisions/{reply_revision_id}/translation-review",
        response_model=InquiryTranslationReviewResponse,
        status_code=201,
    )
    def review_inquiry_translation_route(  # noqa: PLR0913, PLR0917
        inquiry_id: str,
        reply_revision_id: str,
        body: InquiryTranslationReviewRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> InquiryTranslationReviewResponse:
        _require_role(identity, {"reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        with factory() as session, session.begin():
            result = review_inquiry_translation(
                session,
                org_id=identity.org_id,
                inquiry_id=inquiry_id,
                reply_revision_id=reply_revision_id,
                content_hash=body.content_hash,
                decision=TranslationReviewDecision(body.decision),
                actor_id=identity.actor_id,
                actor_role=identity.role,
                idempotency_key=idempotency_key.strip(),
                reason=body.reason,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return InquiryTranslationReviewResponse(
            review_id=result.review_id,
            inquiry_id=result.inquiry_id,
            reply_revision_id=result.reply_revision_id,
            content_hash=result.content_hash,
            decision=result.decision.value,
            idempotent_replay=result.idempotent_replay,
        )

    @application.post(
        "/api/v1/runs",
        response_model=CreateRunResponse,
        status_code=201,
        summary="Create a queued run",
    )
    def create_run_route(
        body: CreateRunRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> CreateRunResponse:
        if identity.role != "sales":
            raise _ApiProblemError(
                403, "forbidden", "Only sales users can create runs."
            )
        try:  # noqa: PLR1702 - transaction and idempotent target checks are nested
            with factory() as session, session.begin():
                existing = session.scalar(
                    select(RunRecord).where(RunRecord.run_id == body.run_id)
                )
                if existing is not None:
                    target = (
                        identity.org_id,
                        body.rfq_id,
                        body.rfq_revision_id,
                        body.graph_version,
                        body.run_id,
                    )
                    actual = (
                        existing.org_id,
                        existing.rfq_id,
                        existing.rfq_revision_id,
                        existing.graph_version,
                        existing.thread_id,
                    )
                    if actual != target:
                        raise RunConflictError(
                            "run_id already targets a different workflow"
                        )
                    result = RunResult(
                        existing.run_id,
                        existing.status,
                        existing.event_seq,
                        idempotent_replay=True,
                    )
                else:
                    authorized_revision(session, body, identity)
                    result = create_run(
                        session,
                        run_id=body.run_id,
                        org_id=identity.org_id,
                        rfq_id=body.rfq_id,
                        rfq_revision_id=body.rfq_revision_id,
                        graph_version=body.graph_version,
                        thread_id=body.run_id,
                        created_by=identity.actor_id,
                    )
        except IntegrityError:
            result = _resolve_create_race(factory, body, identity)
        response.status_code = 200 if result.idempotent_replay else 201
        return CreateRunResponse(
            run_id=result.run_id,
            status=result.status,
            event_seq=result.event_seq,
            idempotent_replay=result.idempotent_replay,
        )

    @application.get("/api/v1/runs/{run_id}", response_model=RunView)
    def get_run_route(
        run_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> RunView:
        _require_role(identity, {"sales", "reviewer"})
        with factory() as session:
            record = authorized_run(session, run_id, identity)
            return _run_view(record)

    @application.get("/api/v1/runs/{run_id}/events", response_model=RunEventPage)
    def get_run_events_route(
        run_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> RunEventPage:
        _require_role(identity, {"sales", "reviewer"})
        with factory() as session:
            authorized_run(session, run_id, identity)
            total = (
                session.scalar(
                    select(func.count())
                    .select_from(RunEventRecord)
                    .where(RunEventRecord.run_id == run_id)
                )
                or 0
            )
            events = list(
                session.scalars(
                    select(RunEventRecord)
                    .where(RunEventRecord.run_id == run_id)
                    .order_by(RunEventRecord.event_seq, RunEventRecord.event_id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
        return RunEventPage(
            run_id=run_id,
            page=page,
            page_size=page_size,
            total=total,
            events=[_event_view(event) for event in events],
        )

    @application.post(
        "/api/v1/runs/{run_id}/resume",
        response_model=CreateRunResponse,
        summary="Resume a run waiting for human input",
        description=(
            "Queues a waiting run for a worker. This endpoint does not invoke LangGraph "
            "or accept business decisions. Idempotency-Key is scoped to organization "
            "and actor."
        ),
    )
    def resume_run_route(  # noqa: PLR0913, PLR0917 - FastAPI injects request dependencies
        run_id: str,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ] = None,
        body: ResumeRunRequest | None = None,
    ) -> CreateRunResponse:
        _require_role(identity, {"sales", "reviewer"})
        body_key = body.event_key if body is not None else None
        if (
            idempotency_key is not None
            and body_key is not None
            and idempotency_key.strip() != body_key.strip()
        ):
            raise _ApiProblemError(
                409,
                "idempotency_conflict",
                "The header and body idempotency keys must match.",
            )
        supplied_key = idempotency_key or body_key
        if supplied_key is None:
            raise _ApiProblemError(
                422,
                "idempotency_key_required",
                "Idempotency-Key or body.event_key is required.",
            )
        supplied_key = supplied_key.strip()
        if not supplied_key:
            raise _ApiProblemError(
                422,
                "idempotency_key_required",
                "Idempotency-Key or body.event_key is required.",
            )
        event_key = _resume_event_key(identity, supplied_key)
        with factory() as session, session.begin():
            authorized_run(session, run_id, identity)
            result = resume_run(
                session,
                run_id,
                actor_id=identity.actor_id,
                event_key=event_key,
            )
        response.status_code = 200 if result.idempotent_replay else 202
        return CreateRunResponse(
            run_id=result.run_id,
            status=result.status,
            event_seq=result.event_seq,
            idempotent_replay=result.idempotent_replay,
        )

    @application.post(
        "/api/v1/quotations/{quotation_id}/revisions/{revision_id}/approval",
        response_model=QuoteApprovalResponse,
        summary="Approve or reject one exact quote revision",
    )
    def decide_quote_route(  # noqa: PLR0913, PLR0917
        quotation_id: str,
        revision_id: str,
        body: QuoteApprovalRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> QuoteApprovalResponse:
        _require_role(identity, {"reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        with factory() as session, session.begin():
            result = decide_quote_revision(
                session,
                org_id=identity.org_id,
                quotation_id=quotation_id,
                revision_id=revision_id,
                content_hash=body.content_hash,
                decision=body.decision,
                actor_id=identity.actor_id,
                idempotency_key=idempotency_key.strip(),
                reason=body.reason,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _approval_response(result)

    @application.post(
        "/api/v1/quotations/{quotation_id}/revisions/{revision_id}/artifacts",
        response_model=QuoteArtifactResponse,
        summary="Create a private file from an approved quote revision",
    )
    def create_artifact_route(  # noqa: PLR0913, PLR0917
        quotation_id: str,
        revision_id: str,
        body: QuoteArtifactRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> QuoteArtifactResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        if idempotency_key is None or not idempotency_key.strip():
            raise _ApiProblemError(
                422, "idempotency_key_required", "Idempotency-Key is required."
            )
        with factory() as session, session.begin():
            result = create_quote_artifact(
                session,
                org_id=identity.org_id,
                quotation_id=quotation_id,
                revision_id=revision_id,
                artifact_type=body.artifact_type,
                actor_id=identity.actor_id,
                idempotency_key=idempotency_key.strip(),
                requested_content_hash=body.content_hash,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _artifact_response(result)

    @application.get(
        "/api/v1/artifacts/{artifact_id}/download",
        summary="Download a ready private quote artifact",
        responses={
            200: {
                "description": "Approved quote PDF",
                "content": {
                    "application/pdf": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    def download_artifact_route(
        artifact_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> Response:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            artifact = get_quote_artifact(
                session, org_id=identity.org_id, artifact_id=artifact_id
            )
        filename = (
            "proforma-invoice-"
            f"{_safe_filename_component(artifact.quotation_id)}-"
            f"{_safe_filename_component(artifact.revision_id)}.pdf"
        )
        return Response(
            content=artifact.content_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-SHA256": artifact.content_sha256 or "",
            },
        )

    @application.post(
        "/api/v1/document-sets",
        response_model=DocumentSetResponse,
        status_code=201,
        summary="Anchor a document set to an approved quote snapshot",
    )
    def create_document_set_route(
        body: DocumentSetCreateRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> DocumentSetResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        key = _required_idempotency_key(idempotency_key)
        with factory() as session, session.begin():
            result = create_document_set(
                session,
                org_id=identity.org_id,
                document_set_id=body.document_set_id,
                quotation_id=body.quotation_id,
                quote_revision_id=body.quote_revision_id,
                approved_content_hash=body.approved_content_hash,
                actor_id=identity.actor_id,
                idempotency_key=key,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _document_set_response(result)

    @application.get(
        "/api/v1/document-sets/{document_set_id}",
        response_model=DocumentSetDetailResponse,
    )
    def get_document_set_route(
        document_set_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> DocumentSetDetailResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            record = get_document_set(
                session, org_id=identity.org_id, document_set_id=document_set_id
            )
            if record is None:
                raise DocumentNotFoundError(document_set_id)
            revisions = list(
                session.scalars(
                    select(DocumentSetRevisionRecord)
                    .where(
                        DocumentSetRevisionRecord.org_id == identity.org_id,
                        DocumentSetRevisionRecord.document_set_id == document_set_id,
                    )
                    .order_by(DocumentSetRevisionRecord.revision_no)
                )
            )
        return DocumentSetDetailResponse(
            **_document_set_response(
                DocumentSetResult(
                    document_set_id=record.document_set_id,
                    quotation_id=record.quotation_id,
                    quote_revision_id=record.quote_revision_id,
                    approved_content_hash=record.approved_content_hash,
                    status=record.status,
                    idempotent_replay=False,
                )
            ).model_dump(),
            revisions=[
                _document_revision_response_from_record(item) for item in revisions
            ],
        )

    @application.post(
        "/api/v1/document-sets/{document_set_id}/revisions",
        response_model=DocumentRevisionResponse,
        status_code=201,
    )
    def append_document_revision_route(  # noqa: PLR0913, PLR0917
        document_set_id: str,
        body: DocumentRevisionRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> DocumentRevisionResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        key = _required_idempotency_key(idempotency_key)
        document = _document_from_request(body)
        with factory() as session, session.begin():
            result = append_document_revision(
                session,
                org_id=identity.org_id,
                document_set_id=document_set_id,
                document_type=ExportDocumentType(body.document_type),
                document=document,
                actor_id=identity.actor_id,
                idempotency_key=key,
                expected_revision_id=body.expected_revision_id,
                status=body.status,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _document_revision_response(result)

    @application.post(
        "/api/v1/document-sets/{document_set_id}/validate",
        response_model=DocumentValidationResponse,
    )
    def validate_document_set_route(
        document_set_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> DocumentValidationResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            result = validate_document_set(
                session, org_id=identity.org_id, document_set_id=document_set_id
            )
        return _document_validation_response(result)

    @application.post(
        "/api/v1/document-sets/{document_set_id}/issue",
        response_model=DocumentSetResponse,
    )
    def issue_document_set_route(
        document_set_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> DocumentSetResponse:
        _require_role(identity, {"reviewer", "admin"})
        with factory() as session, session.begin():
            result = issue_document_set(
                session, org_id=identity.org_id, document_set_id=document_set_id
            )
        return _document_set_response(result)

    @application.post(
        "/api/v1/document-sets/{document_set_id}/ready",
        response_model=DocumentSetResponse,
    )
    def ready_document_set_route(
        document_set_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> DocumentSetResponse:
        _require_role(identity, {"reviewer", "admin"})
        with factory() as session, session.begin():
            result = ready_document_set(
                session, org_id=identity.org_id, document_set_id=document_set_id
            )
        return _document_set_response(result)

    @application.post(
        "/api/v1/document-sets/{document_set_id}/void",
        response_model=DocumentSetResponse,
    )
    def void_document_set_route(
        document_set_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> DocumentSetResponse:
        _require_role(identity, {"reviewer", "admin"})
        with factory() as session, session.begin():
            result = void_document_set(
                session, org_id=identity.org_id, document_set_id=document_set_id
            )
        return _document_set_response(result)

    @application.post(
        "/api/v1/shipment-handoffs",
        response_model=ShipmentHandoffResponse,
        status_code=201,
    )
    def create_shipment_handoff_route(
        body: ShipmentHandoffCreateRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> ShipmentHandoffResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        key = _required_idempotency_key(idempotency_key)
        handoff = ShipmentHandoff(
            shipment_handoff_id=body.shipment_handoff_id,
            org_id=identity.org_id,
            quotation_id=body.quotation_id,
            quote_revision_id=body.quote_revision_id,
            approved_content_hash=body.approved_content_hash,
            selected_incoterm=body.selected_incoterm,
            named_place=body.named_place,
            responsibility_split=body.responsibility_split,
            freight_forwarder_name=body.freight_forwarder_name,
            freight_forwarder_quote_ref=body.freight_forwarder_quote_ref,
            carrier_name=body.carrier_name,
            insurance_scope=body.insurance_scope,
            insurance_expires_at=body.insurance_expires_at,
            document_set_id=body.document_set_id,
            required_documents=tuple(body.required_documents),
        )
        with factory() as session, session.begin():
            result = create_shipment_handoff(
                session,
                handoff,
                actor_id=identity.actor_id,
                idempotency_key=key,
            )
            record = get_shipment_handoff(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=result.shipment_handoff_id,
            )
            if record is None:
                raise ShipmentNotFoundError("shipment handoff was not found")
        response.status_code = 200 if result.idempotent_replay else 201
        return _shipment_handoff_response(record, result=result)

    @application.get(
        "/api/v1/shipment-handoffs/{shipment_handoff_id}",
        response_model=ShipmentHandoffResponse,
    )
    def get_shipment_handoff_route(
        shipment_handoff_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ) -> ShipmentHandoffResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            record = get_shipment_handoff(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
            )
            if record is None:
                raise ShipmentNotFoundError(shipment_handoff_id)
            evidence = list_shipment_evidence(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
            )
            decisions = list_shipment_gate_decisions(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
            )
        return _shipment_handoff_response(
            record, evidence=evidence, decisions=decisions
        )

    @application.post(
        "/api/v1/shipment-handoffs/{shipment_handoff_id}/evidence",
        response_model=ShipmentEvidenceResponse,
        status_code=201,
    )
    def append_shipment_evidence_route(  # noqa: PLR0913, PLR0917
        shipment_handoff_id: str,
        body: ShipmentEvidenceRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> ShipmentEvidenceResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        key = _required_idempotency_key(idempotency_key)
        evidence = ShipmentEvidence(
            evidence_id=body.evidence_id,
            evidence_type=body.evidence_type,
            check_key=body.check_key,
            evidence_ref=body.evidence_ref,
            content_hash=body.content_hash,
            status=body.status,
            notes=body.notes,
            expires_at=body.expires_at,
            parent_evidence_id=body.parent_evidence_id,
        )
        with factory() as session, session.begin():
            result = append_shipment_evidence(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
                evidence=evidence,
                actor_id=identity.actor_id,
                idempotency_key=key,
                expected_evidence_id=body.expected_evidence_id,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _shipment_evidence_response(result)

    @application.get(
        "/api/v1/shipment-handoffs/{shipment_handoff_id}/gate",
        response_model=ShipmentGateResponse,
    )
    def evaluate_shipment_gate_route(
        shipment_handoff_id: str,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        target_status: Annotated[
            Literal["ready_for_booking", "booked", "in_transit"], Query()
        ] = "ready_for_booking",
    ) -> ShipmentGateResponse:
        _require_role(identity, {"sales", "reviewer", "admin"})
        with factory() as session:
            result = evaluate_shipment_gate(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
                target_status=target_status,
            )
        return _shipment_gate_response(result)

    @application.post(
        "/api/v1/shipment-handoffs/{shipment_handoff_id}/transitions",
        response_model=ShipmentTransitionResponse,
        status_code=201,
    )
    def transition_shipment_handoff_route(  # noqa: PLR0913, PLR0917
        shipment_handoff_id: str,
        body: ShipmentTransitionRequest,
        response: Response,
        identity: Annotated[_Identity, Depends(require_identity)],
        factory: Annotated[SessionFactory, Depends(get_session_factory)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> ShipmentTransitionResponse:
        target = body.target_status.value
        if target in {
            ShipmentHandoffStatus.READY_FOR_BOOKING.value,
            ShipmentHandoffStatus.BOOKED.value,
            ShipmentHandoffStatus.BLOCKED.value,
        }:
            _require_role(identity, {"reviewer", "admin"})
        else:
            _require_role(identity, {"sales", "reviewer", "admin"})
        key = _required_idempotency_key(idempotency_key)
        with factory() as session, session.begin():
            result = transition_shipment_handoff(
                session,
                org_id=identity.org_id,
                shipment_handoff_id=shipment_handoff_id,
                target_status=target,
                actor_id=identity.actor_id,
                actor_role=identity.role,
                idempotency_key=key,
                reason=body.reason,
                override=body.override,
                booking_reference=body.booking_reference,
                eta=body.eta,
            )
        response.status_code = 200 if result.idempotent_replay else 201
        return _shipment_transition_response(result)

    return application


def _required_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise _ApiProblemError(
            422, "idempotency_key_required", "Idempotency-Key is required."
        )
    return value.strip()


def _document_from_request(
    body: DocumentRevisionRequest,
) -> CommercialInvoiceDocument | PackingListDocument:
    if body.document_type == ExportDocumentType.COMMERCIAL_INVOICE.value:
        if body.currency is None:
            raise _ApiProblemError(
                422, "invalid_request", "currency is required for a commercial invoice."
            )
        return CommercialInvoiceDocument(
            buyer=body.buyer,
            consignee=body.consignee,
            document_reference=body.document_reference,
            currency=body.currency,
            incoterm=body.incoterm,
            named_place=body.named_place,
            lines=tuple(
                CommercialInvoiceLine(**line.model_dump()) for line in body.lines
            ),
        )
    return PackingListDocument(
        buyer=body.buyer,
        consignee=body.consignee,
        document_reference=body.document_reference,
        incoterm=body.incoterm,
        named_place=body.named_place,
        packages=tuple(
            PackingListPackage(**package.model_dump()) for package in body.packages
        ),
    )


def _document_set_response(result: DocumentSetResult) -> DocumentSetResponse:
    return DocumentSetResponse(
        document_set_id=result.document_set_id,
        quotation_id=result.quotation_id,
        quote_revision_id=result.quote_revision_id,
        approved_content_hash=result.approved_content_hash,
        status=result.status,
        idempotent_replay=result.idempotent_replay,
    )


def _document_revision_response(
    result: DocumentRevisionResult,
) -> DocumentRevisionResponse:
    return DocumentRevisionResponse(
        document_set_id=result.document_set_id,
        revision_id=result.revision_id,
        revision_no=result.revision_no,
        document_type=result.document_type,
        status=result.status,
        content_hash=result.content_hash,
        idempotent_replay=result.idempotent_replay,
    )


def _document_revision_response_from_record(
    record: DocumentSetRevisionRecord,
) -> DocumentRevisionResponse:
    return DocumentRevisionResponse(
        document_set_id=record.document_set_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        document_type=record.document_type,
        status=record.status,
        content_hash=record.content_hash,
        idempotent_replay=False,
    )


def _document_validation_response(
    result: DocumentValidationResult,
) -> DocumentValidationResponse:
    return DocumentValidationResponse(
        document_set_id=result.document_set_id,
        valid=result.valid,
        ready=result.ready,
        issues=[
            DocumentIssueView(code=item.code, field=item.field, message=item.message)
            for item in result.issues
        ],
    )


def _shipment_handoff_response(
    record: ShipmentHandoffRecord,
    *,
    result: ShipmentHandoffResult | None = None,
    evidence: list[ShipmentHandoffEvidenceRecord] | None = None,
    decisions: list[ShipmentHandoffGateDecisionRecord] | None = None,
) -> ShipmentHandoffResponse:
    return ShipmentHandoffResponse(
        shipment_handoff_id=record.shipment_handoff_id,
        quotation_id=record.quotation_id,
        quote_revision_id=record.quote_revision_id,
        approved_content_hash=record.approved_content_hash,
        document_set_id=record.document_set_id,
        selected_incoterm=record.selected_incoterm,
        named_place=record.named_place,
        responsibility_split=record.responsibility_split,
        freight_forwarder_name=record.freight_forwarder_name,
        freight_forwarder_quote_ref=record.freight_forwarder_quote_ref,
        carrier_name=record.carrier_name,
        insurance_scope=record.insurance_scope,
        insurance_expires_at=record.insurance_expires_at,
        required_documents=record.required_documents,
        booking_reference=record.booking_reference,
        eta=record.eta,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        created_by=record.created_by,
        idempotent_replay=result.idempotent_replay if result else False,
        evidence=[_shipment_evidence_view(item) for item in evidence or []],
        gate_decisions=[
            ShipmentGateDecisionView(
                decision_id=item.decision_id,
                target_status=item.target_status,
                decision=item.decision,
                gate_hash=item.gate_hash,
                overridden=item.overridden,
                reason=item.reason,
                actor_id=item.actor_id,
                actor_role=item.actor_role,
                decided_at=item.decided_at,
            )
            for item in decisions or []
        ],
    )


def _shipment_evidence_view(
    record: ShipmentHandoffEvidenceRecord,
) -> ShipmentEvidenceView:
    return ShipmentEvidenceView(
        evidence_id=record.evidence_id,
        revision_no=record.revision_no,
        evidence_type=record.evidence_type,
        check_key=record.check_key,
        evidence_ref=record.evidence_ref,
        content_hash=record.content_hash,
        status=record.status,
        notes=record.notes,
        expires_at=record.expires_at,
        parent_evidence_id=record.parent_evidence_id,
        created_at=record.created_at,
        actor_id=record.actor_id,
    )


def _shipment_evidence_response(
    result: ShipmentEvidenceResult,
) -> ShipmentEvidenceResponse:
    return ShipmentEvidenceResponse(
        shipment_handoff_id=result.shipment_handoff_id,
        evidence_id=result.evidence_id,
        revision_no=result.revision_no,
        evidence_type=result.evidence_type,
        check_key=result.check_key,
        status=result.status,
        content_hash=result.content_hash,
        idempotent_replay=result.idempotent_replay,
    )


def _shipment_gate_response(result: ShipmentGateResult) -> ShipmentGateResponse:
    return ShipmentGateResponse(
        shipment_handoff_id=result.shipment_handoff_id,
        valid=result.valid,
        gate_hash=result.gate_hash,
        issues=list(result.issues),
    )


def _shipment_transition_response(
    result: ShipmentTransitionResult,
) -> ShipmentTransitionResponse:
    return ShipmentTransitionResponse(
        shipment_handoff_id=result.shipment_handoff_id,
        status=result.status,
        target_status=result.target_status,
        decision_id=result.decision_id,
        gate_hash=result.gate_hash,
        idempotent_replay=result.idempotent_replay,
    )


def _qualification_check_view(
    record: QualificationCheckRecord, *, reviewer_id: str | None = None
) -> QualificationCheckView:
    return QualificationCheckView(
        check_id=record.check_id,
        org_id=record.org_id,
        customer_id=record.customer_id,
        inquiry_id=record.inquiry_id,
        source=record.source,
        reference=record.reference,
        checked_at=record.checked_at,
        result=record.result,
        evidence_hash=record.evidence_hash,
        evidence_attachment_ref=record.evidence_attachment_ref,
        notes=record.notes,
        reviewer_id=reviewer_id if reviewer_id is not None else record.reviewer_id,
        created_at=record.created_at,
        created_by=record.created_by,
    )


def _qualification_decision_view(
    record: object,
) -> QualificationDecisionView:
    # The narrow protocol avoids exposing SQLAlchemy classes in the API contract.
    return QualificationDecisionView(
        decision_id=record.decision_id,  # type: ignore[attr-defined]
        check_id=record.check_id,  # type: ignore[attr-defined]
        result=record.result,  # type: ignore[attr-defined]
        evidence_hash=record.evidence_hash,  # type: ignore[attr-defined]
        reviewer_id=record.actor_id,  # type: ignore[attr-defined]
        notes=record.notes,  # type: ignore[attr-defined]
        decided_at=record.decided_at,  # type: ignore[attr-defined]
    )


def _qualification_list_item_view(
    item: QualificationView,
) -> QualificationListItemView:
    decision = (
        _qualification_decision_view(item.decision)
        if item.decision is not None
        else None
    )
    reviewer_id = decision.reviewer_id if decision is not None else None
    return QualificationListItemView(
        **_qualification_check_view(item.check, reviewer_id=reviewer_id).model_dump(),
        decision=decision,
        effective_result=item.effective_result.value,
    )


def _qualification_detail_view(item: QualificationView) -> QualificationDetailView:
    decision = (
        _qualification_decision_view(item.decision)
        if item.decision is not None
        else None
    )
    reviewer_id = decision.reviewer_id if decision is not None else None
    return QualificationDetailView(
        check=_qualification_check_view(item.check, reviewer_id=reviewer_id),
        decision=decision,
        effective_result=item.effective_result.value,
    )


def _qualification_check_response(
    result: QualificationWriteResult,
) -> QualificationCheckResponse:
    return QualificationCheckResponse(
        check_id=result.check_id,
        result=result.result.value,
        evidence_hash=result.evidence_hash,
        idempotent_replay=result.idempotent_replay,
    )


def _qualification_decision_response(
    result: QualificationDecisionResult,
) -> QualificationDecisionResponse:
    return QualificationDecisionResponse(
        decision_id=result.decision_id,
        check_id=result.check_id,
        result=result.result.value,
        evidence_hash=result.evidence_hash,
        reviewer_id=result.reviewer_id,
        idempotent_replay=result.idempotent_replay,
    )


def _inquiry_queue_item_view(item: InquiryQueueItem) -> InquiryQueueItemView:
    return _inquiry_case_view(item.record, item.queue_state, item.is_overdue)


def _inquiry_case_view(
    record: InquiryCaseRecord,
    queue_state: str,
    is_overdue: bool,  # noqa: FBT001
) -> InquiryCaseView:
    return InquiryCaseView(
        inquiry_id=record.inquiry_id,
        rfq_id=record.rfq_id,
        source_channel=record.source_channel,
        customer_role=record.customer_role,
        original_language=record.original_language,
        received_at=record.received_at,
        response_due_at=record.response_due_at,
        owner_id=record.owner_id,
        queue_state=queue_state,
        is_overdue=is_overdue,
        current_reply_revision_id=record.current_reply_revision_id,
        current_reply_revision_no=record.current_reply_revision_no,
        created_at=record.created_at,
        created_by=record.created_by,
        attachments=record.attachments,
    )


def _require_role(identity: _Identity, allowed: set[str]) -> None:
    if identity.role not in allowed:
        raise _ApiProblemError(403, "forbidden", "The caller role is not allowed.")


def _approval_response(result: ApprovalResult) -> QuoteApprovalResponse:
    return QuoteApprovalResponse(
        approval_id=result.approval_id,
        quotation_id=result.quotation_id,
        revision_id=result.revision_id,
        content_hash=result.content_hash,
        decision=result.decision,
        idempotent_replay=result.idempotent_replay,
    )


def _safe_filename_component(value: str) -> str:
    """Keep object IDs out of Content-Disposition syntax."""
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return component[:128] or "quote"


def _artifact_response(result: ArtifactResult) -> QuoteArtifactResponse:
    return QuoteArtifactResponse(
        artifact_id=result.artifact_id,
        quotation_id=result.quotation_id,
        revision_id=result.revision_id,
        artifact_type=result.artifact_type,
        status=result.status,
        content_hash=result.content_hash,
        amount_total=format(result.amount_total, ".2f"),
        content_sha256=result.content_sha256,
        idempotent_replay=result.idempotent_replay,
    )


def _resume_event_key(identity: _Identity, idempotency_key: str) -> str:
    scope = f"{identity.org_id}\0{identity.actor_id}\0{idempotency_key}"
    return f"api-resume:{sha256(scope.encode('utf-8')).hexdigest()}"


def _resolve_create_race(
    factory: SessionFactory,
    body: CreateRunRequest,
    identity: _Identity,
) -> RunResult:
    with factory() as session:
        try:
            record = get_run(session, body.run_id)
        except RunNotFoundError:
            raise
        target = (
            identity.org_id,
            body.rfq_id,
            body.rfq_revision_id,
            body.graph_version,
            body.run_id,
        )
        existing = (
            record.org_id,
            record.rfq_id,
            record.rfq_revision_id,
            record.graph_version,
            record.thread_id,
        )
        if existing != target:
            raise RunConflictError("run_id already targets a different workflow")
        return RunResult(
            record.run_id, record.status, record.event_seq, idempotent_replay=True
        )


def _run_view(record: RunRecord) -> RunView:
    return RunView(
        run_id=record.run_id,
        status=record.status,
        rfq_id=record.rfq_id,
        rfq_revision_id=record.rfq_revision_id,
        graph_version=record.graph_version,
        thread_id=record.thread_id,
        wait_reason=record.wait_reason,
        error_code=record.error_code,
        event_seq=record.event_seq,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _event_view(event: RunEventRecord) -> RunEventView:
    return RunEventView(
        event_id=event.event_id,
        event_seq=event.event_seq,
        event_type=event.event_type,
        node_name=event.node_name,
        occurred_at=event.occurred_at,
    )


def _error_response(
    request: Request, status_code: int, code: str, message: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", str(uuid4())),
        },
    )


app = create_app()
