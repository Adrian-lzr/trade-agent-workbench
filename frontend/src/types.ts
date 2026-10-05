export type UserRole = "sales" | "reviewer" | "admin";

export type RunStatus =
  | "queued"
  | "running"
  | "waiting_input"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface RunView {
  run_id: string;
  status: RunStatus;
  rfq_id: string;
  rfq_revision_id: string;
  graph_version: string;
  thread_id: string;
  wait_reason: string | null;
  error_code: string | null;
  event_seq: number;
  created_at: string;
  updated_at: string;
}

export interface RunEvent {
  event_id: string;
  event_seq: number;
  event_type: string;
  node_name: string | null;
  occurred_at: string;
}

export type InquiryCustomerRole = "end_customer" | "representative" | "distributor" | "unknown";
export type InquiryQueueState = "open" | "overdue" | "responded" | "nurture" | "closed";
export type InquiryTranslationReviewState = "not_required" | "pending" | "approved" | "rejected";

export interface InquiryAttachmentView {
  attachment_id: string;
  filename: string;
  media_type: string;
  storage_ref: string;
  content_sha256: string;
  size_bytes: number | null;
}

export interface InquiryQueueItem {
  inquiry_id: string;
  rfq_id: string;
  source_channel: string;
  customer_role: InquiryCustomerRole;
  original_language: string;
  received_at: string;
  response_due_at: string;
  owner_id: string | null;
  queue_state: InquiryQueueState;
  is_overdue: boolean;
  current_reply_revision_id: string | null;
  current_reply_revision_no: number | null;
}

export interface InquiryCaseView extends InquiryQueueItem {
  created_at: string;
  created_by: string;
  attachments: InquiryAttachmentView[];
}

export interface InquiryReplyRevisionView {
  reply_revision_id: string;
  revision_no: number;
  rfq_revision_id: string;
  source_language: string;
  target_language: string;
  source_content: string;
  translated_content: string | null;
  template_version: string;
  attachments: InquiryAttachmentView[];
  content_hash: string;
  created_at: string;
  created_by: string;
}

export interface InquiryTranslationReviewView {
  review_id: string;
  content_hash: string;
  decision: "approved" | "rejected";
  actor_id: string;
  reason: string | null;
  reviewed_at: string;
}

export interface InquiryDetailResponse {
  case: InquiryCaseView;
  current_reply: InquiryReplyRevisionView | null;
  translation_review: InquiryTranslationReviewView | null;
  translation_review_state: InquiryTranslationReviewState;
}

export interface InquiryQueueResponse {
  items: InquiryQueueItem[];
  limit: number;
  offset: number;
}

export interface CreateInquiryReplyRevisionRequest {
  rfq_revision_id: string;
  source_language: string;
  target_language: string;
  source_content: string;
  translated_content?: string | null;
  template_version?: string;
  expected_content_hash?: string | null;
}

export interface CreateInquiryReplyRevisionResponse {
  inquiry_id: string;
  reply_revision_id: string;
  revision_no: number;
  content_hash: string;
  idempotent_replay: boolean;
}

export interface InquiryTranslationReviewRequest {
  content_hash: string;
  decision: "approved" | "rejected";
  reason?: string | null;
}

export interface InquiryTranslationReviewResponse {
  review_id: string;
  inquiry_id: string;
  reply_revision_id: string;
  content_hash: string;
  decision: "approved" | "rejected";
  idempotent_replay: boolean;
}

export type QualificationCheckResult =
  | "clear"
  | "potential_match"
  | "blocked"
  | "unverified";

export interface QualificationDecisionView {
  decision_id: string;
  check_id: string;
  result: QualificationCheckResult;
  reviewer_id: string;
  notes: string | null;
  decided_at: string;
  evidence_hash: string;
}

export interface QualificationCheckView {
  check_id: string;
  org_id?: string;
  customer_id: string | null;
  inquiry_id: string | null;
  source: string;
  reference: string | null;
  checked_at: string;
  result: QualificationCheckResult;
  evidence_hash: string;
  evidence_attachment_ref: string | null;
  notes: string | null;
  reviewer_id: string | null;
  created_at: string;
  created_by: string;
  decision?: QualificationDecisionView | null;
  effective_result?: QualificationCheckResult;
}

export interface QualificationCheckListResponse {
  items: QualificationCheckView[];
  limit: number;
  offset: number;
}

export interface QualificationCheckResponse {
  check_id: string;
  result: QualificationCheckResult;
  evidence_hash: string;
  idempotent_replay: boolean;
}

export interface Inquiry {
  id: string;
  customer: string;
  country: string;
  subject: string;
  received: string;
  owner: string;
  status:
    | "needs_review"
    | "waiting_input"
    | "in_progress"
    | "ready"
    | InquiryQueueState;
  quote: string;
  revision: string;
  runId: string;
  run?: RunView;
  events?: RunEvent[];
  apiCase?: InquiryCaseView;
  currentReply?: InquiryReplyRevisionView | null;
  translationReview?: InquiryTranslationReviewView | null;
  translationReviewState?: InquiryTranslationReviewState;
  qualificationChecks?: QualificationCheckView[];
  qualificationChecksLoaded?: boolean;
  qualificationChecksError?: string | null;
}

export interface QuoteRevision {
  id: string;
  quoteId: string;
  version: string;
  createdAt: string;
  createdBy: string;
  status: "draft" | "in_review" | "approved" | "rejected" | "superseded";
  contentHash: string;
  amount: string;
  currency: string;
  validity: string;
  items: Array<{
    sku: string;
    description: string;
    quantity: string;
    unit: string;
    unitPrice: string;
    total: string;
  }>;
  blockers: Array<{
    code: string;
    title: string;
    detail: string;
    severity: "blocking" | "warning";
  }>;
}

export interface QuoteApprovalResponse {
  approval_id: string;
  quotation_id: string;
  revision_id: string;
  content_hash: string;
  decision: "approved" | "rejected";
  idempotent_replay: boolean;
}

export interface QuoteArtifactResponse {
  artifact_id: string;
  quotation_id: string;
  revision_id: string;
  artifact_type: "proforma_invoice";
  status: string;
  content_hash: string;
  amount_total: string;
  content_sha256: string | null;
  idempotent_replay: boolean;
}

export type AnalyticsMetricName =
  | "rfq_count"
  | "quote_revision_count"
  | "run_success_rate";

export interface AnalyticsPoint {
  dimensions: Record<string, string | number | null>;
  value: number;
}

export interface AnalyticsResult {
  mode: "fake" | "real";
  source: string;
  is_synthetic: boolean;
  metric: AnalyticsMetricName;
  dimensions: string[];
  definition: {
    name: AnalyticsMetricName;
    label: string;
    definition: string;
    formula: string;
    source_view: string;
  };
  points: AnalyticsPoint[];
}

export interface MetricCard {
  label: string;
  value: string;
  change: string;
  trend: "up" | "down" | "flat";
  note: string;
}
