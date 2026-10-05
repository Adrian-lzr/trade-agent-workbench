import type {
  AnalyticsMetricName,
  AnalyticsResult,
  CreateInquiryReplyRevisionRequest,
  CreateInquiryReplyRevisionResponse,
  Inquiry,
  InquiryDetailResponse,
  InquiryQueueResponse,
  QualificationCheckListResponse,
  InquiryTranslationReviewRequest,
  InquiryTranslationReviewResponse,
  QuoteApprovalResponse,
  QuoteArtifactResponse,
  QuoteRevision,
  RunEvent,
  RunView,
} from "../types";

const rawApiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const apiBase = rawApiBase.replace(/\/$/, "");
const apiMode = import.meta.env.VITE_API_MODE ?? "demo";
const trustedRole = import.meta.env.VITE_TRUSTED_ROLE ?? "";
const trustedOrgId = import.meta.env.VITE_TRUSTED_ORG_ID ?? "";
const trustedActorId = import.meta.env.VITE_TRUSTED_ACTOR_ID ?? "";
const configuredQuoteTarget = {
  quotationId: import.meta.env.VITE_QUOTATION_ID ?? "",
  revisionId: import.meta.env.VITE_QUOTE_REVISION_ID ?? "",
  contentHash: import.meta.env.VITE_QUOTE_CONTENT_HASH ?? "",
};

export const apiConfig = {
  enabled: apiMode === "api",
  baseUrl: apiBase,
  orgId: trustedOrgId,
  actorId: trustedActorId,
  role: trustedRole,
  usesTrustedHeaders: Boolean(trustedOrgId && trustedActorId && trustedRole),
  hasQuoteTarget: Boolean(
    configuredQuoteTarget.quotationId &&
      configuredQuoteTarget.revisionId &&
      /^[a-f0-9]{64}$/.test(configuredQuoteTarget.contentHash),
  ),
  quoteTarget: configuredQuoteTarget,
};

const approvalRoles = new Set(["reviewer", "admin"]);
const artifactRoles = new Set(["sales", "reviewer", "admin"]);
const inquiryWriteRoles = new Set(["sales"]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function identityHeaders(): HeadersInit {
  if (!apiConfig.usesTrustedHeaders) return { Accept: "application/json" };
  return {
    Accept: "application/json",
    "X-Org-Id": trustedOrgId,
    "X-Actor-Id": trustedActorId,
    "X-Role": trustedRole,
  };
}

function requireInquiryApi(roleSet: Set<string>): void {
  if (!apiConfig.enabled) {
    throw new ApiError(403, "演示模式不会读取或写入服务端询盘。", "inquiry_api_unavailable");
  }
  if (!apiConfig.usesTrustedHeaders) {
    throw new ApiError(401, "询盘 API 需要部署提供的受信身份。", "identity_required");
  }
  if (!roleSet.has(trustedRole)) {
    throw new ApiError(403, "当前受信角色无权执行此询盘操作。", "forbidden");
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const requestHeaders = new Headers(identityHeaders());
  if (init.body !== undefined) requestHeaders.set("Content-Type", "application/json");
  if (init.headers) {
    new Headers(init.headers).forEach((value, key) => requestHeaders.set(key, value));
  }
  const response = await fetch(`${apiBase}${path}`, { ...init, headers: requestHeaders });
  if (!response.ok) {
    let message = `API ${response.status}: ${response.statusText}`;
    let code: string | null = null;
    try {
      const problem = (await response.json()) as {
        code?: string;
        message?: string;
        detail?: string;
      };
      code = problem.code ?? null;
      message = problem.message ?? problem.detail ?? message;
    } catch {
      // Keep the HTTP status when a proxy returns a non-JSON error.
    }
    throw new ApiError(response.status, message, code);
  }
  return (await response.json()) as T;
}

function targetForRevision(revision: QuoteRevision) {
  if (!apiConfig.enabled || !apiConfig.usesTrustedHeaders || !apiConfig.hasQuoteTarget) {
    return null;
  }
  if (
    configuredQuoteTarget.quotationId !== revision.quoteId ||
    configuredQuoteTarget.revisionId !== revision.id ||
    configuredQuoteTarget.contentHash !== revision.contentHash
  ) {
    return null;
  }
  return configuredQuoteTarget;
}

export function canSubmitQuoteDecision(revision: QuoteRevision): boolean {
  return Boolean(targetForRevision(revision) && approvalRoles.has(trustedRole));
}

export function canCreateQuoteArtifact(revision: QuoteRevision): boolean {
  return Boolean(targetForRevision(revision) && artifactRoles.has(trustedRole));
}

export function quoteTargetStatus(revision: QuoteRevision): string {
  if (!apiConfig.enabled) return "当前为演示模式。";
  if (!apiConfig.usesTrustedHeaders) {
    return "API 模式需要部署提供的受信身份，浏览器不能自行切换角色。";
  }
  if (!apiConfig.hasQuoteTarget) {
    return "API 模式需要 VITE_QUOTATION_ID、VITE_QUOTE_REVISION_ID 和完整 SHA-256 摘要。";
  }
  if (!targetForRevision(revision)) {
    return "当前演示版本与配置的服务端报价目标不一致，操作已锁定。";
  }
  if (!approvalRoles.has(trustedRole)) return "当前受信角色没有审批权限。";
  return "当前版本已绑定服务端 quotation、revision 和 content hash。";
}

export async function hydrateInquiry(inquiry: Inquiry): Promise<Inquiry> {
  if (!apiConfig.enabled || !apiConfig.usesTrustedHeaders) return inquiry;
  try {
    const [run, events] = await Promise.all([
      request<RunView>(`/api/v1/runs/${encodeURIComponent(inquiry.runId)}`),
      request<{ events: RunEvent[] }>(
        `/api/v1/runs/${encodeURIComponent(inquiry.runId)}/events?page=1&page_size=50`,
      ),
    ]);
    return { ...inquiry, run, events: events.events };
  } catch {
    return inquiry;
  }
}

export async function listInquiryQueue(options: {
  includeClosed?: boolean;
  limit?: number;
  offset?: number;
} = {}): Promise<InquiryQueueResponse> {
  requireInquiryApi(artifactRoles);
  const params = new URLSearchParams({
    include_closed: String(options.includeClosed ?? false),
    limit: String(options.limit ?? 100),
    offset: String(options.offset ?? 0),
  });
  return request<InquiryQueueResponse>(`/api/v1/inquiries/queue?${params.toString()}`);
}

export async function getInquiryDetail(inquiryId: string): Promise<InquiryDetailResponse> {
  requireInquiryApi(artifactRoles);
  return request<InquiryDetailResponse>(`/api/v1/inquiries/${encodeURIComponent(inquiryId)}`);
}

export async function listQualificationChecks(options: {
  inquiryId?: string;
  customerId?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<QualificationCheckListResponse> {
  requireInquiryApi(artifactRoles);
  const params = new URLSearchParams({
    limit: String(options.limit ?? 100),
    offset: String(options.offset ?? 0),
  });
  if (options.inquiryId) params.set("inquiry_id", options.inquiryId);
  if (options.customerId) params.set("customer_id", options.customerId);
  return request<QualificationCheckListResponse>(
    `/api/v1/qualification-checks?${params.toString()}`,
  );
}

export function canReviewInquiryTranslation(inquiry: Inquiry): boolean {
  return Boolean(
    apiConfig.enabled &&
      apiConfig.usesTrustedHeaders &&
      approvalRoles.has(trustedRole) &&
      inquiry.apiCase &&
      inquiry.currentReply &&
      inquiry.translationReviewState === "pending" &&
      inquiry.currentReply.created_by !== trustedActorId,
  );
}

export async function createInquiryReplyRevision(
  inquiryId: string,
  body: CreateInquiryReplyRevisionRequest,
  idempotencyKey: string,
): Promise<CreateInquiryReplyRevisionResponse> {
  requireInquiryApi(inquiryWriteRoles);
  if (!idempotencyKey.trim()) {
    throw new ApiError(422, "创建回复版本需要幂等键。", "idempotency_key_required");
  }
  return request<CreateInquiryReplyRevisionResponse>(
    `/api/v1/inquiries/${encodeURIComponent(inquiryId)}/reply-revisions`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    },
  );
}

export async function reviewInquiryTranslation(
  inquiryId: string,
  replyRevisionId: string,
  body: InquiryTranslationReviewRequest,
): Promise<InquiryTranslationReviewResponse> {
  requireInquiryApi(approvalRoles);
  const idempotencyKey =
    `trade-ui:translation-review:${inquiryId}:${replyRevisionId}:` +
    `${body.content_hash}:${body.decision}`;
  return request<InquiryTranslationReviewResponse>(
    `/api/v1/inquiries/${encodeURIComponent(inquiryId)}/reply-revisions/${encodeURIComponent(replyRevisionId)}/translation-review`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    },
  );
}

export async function submitQuoteDecision(
  revision: QuoteRevision,
  decision: "approved" | "rejected",
  reason: string | null,
): Promise<QuoteApprovalResponse> {
  const target = targetForRevision(revision);
  if (!target || !approvalRoles.has(trustedRole)) {
    throw new ApiError(403, quoteTargetStatus(revision), "quote_target_unavailable");
  }
  return request<QuoteApprovalResponse>(
    `/api/v1/quotations/${encodeURIComponent(target.quotationId)}/revisions/${encodeURIComponent(target.revisionId)}/approval`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": `trade-ui:approval:${target.revisionId}:${target.contentHash}:${decision}`,
      },
      body: JSON.stringify({ content_hash: target.contentHash, decision, reason }),
    },
  );
}

export async function createQuoteArtifact(
  revision: QuoteRevision,
): Promise<QuoteArtifactResponse> {
  const target = targetForRevision(revision);
  if (!target || !artifactRoles.has(trustedRole)) {
    throw new ApiError(403, quoteTargetStatus(revision), "quote_target_unavailable");
  }
  return request<QuoteArtifactResponse>(
    `/api/v1/quotations/${encodeURIComponent(target.quotationId)}/revisions/${encodeURIComponent(target.revisionId)}/artifacts`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": `trade-ui:artifact:${target.revisionId}:${target.contentHash}:proforma_invoice`,
      },
      body: JSON.stringify({ artifact_type: "proforma_invoice", content_hash: target.contentHash }),
    },
  );
}

export async function downloadQuoteArtifact(artifactId: string): Promise<Blob> {
  if (!apiConfig.enabled || !apiConfig.usesTrustedHeaders) {
    throw new ApiError(403, "演示模式没有可下载的服务端文件。", "artifact_target_unavailable");
  }
  const response = await fetch(
    `${apiBase}/api/v1/artifacts/${encodeURIComponent(artifactId)}/download`,
    { headers: identityHeaders() },
  );
  if (!response.ok) {
    throw new ApiError(response.status, `文件下载失败（HTTP ${response.status}）。`);
  }
  return response.blob();
}

export async function queryAnalytics(
  metric: AnalyticsMetricName,
  dimensions: string[] = [],
): Promise<AnalyticsResult> {
  if (!apiConfig.enabled || !apiConfig.usesTrustedHeaders) {
    throw new ApiError(401, "API 分析需要受信组织和角色身份。", "analytics_identity_required");
  }
  return request<AnalyticsResult>("/api/v1/analytics/query", {
    method: "POST",
    body: JSON.stringify({
      plan: {
        metric,
        dimensions,
        filters: { org_id: trustedOrgId },
        limit: 100,
      },
    }),
  });
}

export function apiModeLabel(): string {
  if (apiConfig.enabled && apiConfig.usesTrustedHeaders) return "已连接 API";
  if (apiConfig.enabled) return "API 等待受信身份";
  return "演示数据";
}
