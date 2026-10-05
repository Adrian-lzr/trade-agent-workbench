<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ArrowRight, FilePlus2, Filter, Paperclip, RefreshCw, Search, ShieldAlert, ShieldCheck } from "lucide-vue-next";
import { useRouter } from "vue-router";
import PageHeader from "../components/PageHeader.vue";
import StatusBadge from "../components/StatusBadge.vue";
import Callout from "../components/Callout.vue";
import { inquiries as seedInquiries } from "../data/demo";
import {
  apiConfig,
  canReviewInquiryTranslation,
  getInquiryDetail,
  hydrateInquiry,
  listQualificationChecks,
  listInquiryQueue,
  reviewInquiryTranslation,
} from "../services/api";
import type {
  Inquiry,
  InquiryQueueItem,
  InquiryQueueState,
  QualificationCheckResult,
  QualificationCheckView,
} from "../types";

const router = useRouter();
const inquiries = ref<Inquiry[]>(seedInquiries.map((item) => ({ ...item })));
const selectedId = ref(seedInquiries[0].id);
const search = ref("");
const filter = ref<string>("all");
const refreshing = ref(false);
const showIntake = ref(false);
const intakeText = ref("");
const intakeMessage = ref("");
const apiQueueLoaded = ref(false);
const sourceMessage = ref("");
const sourceTone = ref<"info" | "warning">("info");
const actionMessage = ref("");
const actionTone = ref<"info" | "warning">("info");
const reviewReason = ref("");

const filterTabs = computed(() => apiQueueLoaded.value
  ? [["all", "全部"], ["overdue", "已逾期"], ["open", "待响应"], ["responded", "已响应"], ["nurture", "跟进中"]] as const
  : [["all", "全部"], ["needs_review", "待审核"], ["waiting_input", "待补充"], ["ready", "可提交"]] as const);

const filteredInquiries = computed(() => {
  const query = search.value.trim().toLowerCase();
  return inquiries.value.filter((item) => {
    const matchesFilter = filter.value === "all"
      || (filter.value === "overdue" ? isOverdue(item) : (item.apiCase?.queue_state ?? item.status) === filter.value);
    const matchesSearch = !query || `${item.id} ${item.customer} ${item.subject}`.toLowerCase().includes(query);
    return matchesFilter && matchesSearch;
  });
});

const selected = computed(() => inquiries.value.find((item) => item.id === selectedId.value) ?? inquiries.value[0]);

async function refreshSelected() {
  if (!selected.value) return;
  refreshing.value = true;
  const selectedIdAtStart = selected.value.id;
  try {
    const index = inquiries.value.findIndex((item) => item.id === selectedIdAtStart);
    if (selected.value.apiCase) {
      const detail = await getInquiryDetail(selectedIdAtStart);
      if (index >= 0 && selectedId.value === selectedIdAtStart) {
        inquiries.value[index] = mapInquiry(detail.case, detail);
      }
      await refreshQualificationChecks(selectedIdAtStart);
    } else if (index >= 0) {
      inquiries.value[index] = await hydrateInquiry(selected.value);
    }
  } catch (error) {
    actionTone.value = "warning";
    actionMessage.value = error instanceof Error ? error.message : "刷新询盘详情失败。";
  } finally {
    refreshing.value = false;
  }
}

async function refreshQualificationChecks(inquiryId: string) {
  const index = inquiries.value.findIndex((item) => item.id === inquiryId);
  if (index < 0) return;
  if (!apiConfig.enabled || !apiConfig.usesTrustedHeaders) {
    inquiries.value[index] = {
      ...inquiries.value[index],
      qualificationChecksLoaded: false,
      qualificationChecksError: null,
    };
    return;
  }
  try {
    const result = await listQualificationChecks({ inquiryId });
    if (selectedId.value !== inquiryId) return;
    inquiries.value[index] = {
      ...inquiries.value[index],
      qualificationChecks: result.items,
      qualificationChecksLoaded: true,
      qualificationChecksError: null,
    };
  } catch (error) {
    if (selectedId.value !== inquiryId) return;
    inquiries.value[index] = {
      ...inquiries.value[index],
      qualificationChecks: [],
      qualificationChecksLoaded: true,
      qualificationChecksError: error instanceof Error ? error.message : "读取筛查记录失败。",
    };
  }
}

function mapInquiry(item: InquiryQueueItem, detail?: Awaited<ReturnType<typeof getInquiryDetail>>): Inquiry {
  const caseView = detail?.case ?? { ...item, created_at: item.received_at, created_by: "", attachments: [] };
  const reply = detail?.current_reply ?? null;
  return {
    id: item.inquiry_id,
    customer: "客户信息待补录",
    country: "地区待补录",
    subject: `RFQ ${item.rfq_id}`,
    received: formatDate(item.received_at),
    owner: item.owner_id ?? "未分配",
    status: item.queue_state,
    quote: item.rfq_id,
    revision: reply ? `R${reply.revision_no}` : "无回复",
    runId: "",
    apiCase: caseView,
    currentReply: reply,
    translationReview: detail?.translation_review ?? null,
    translationReviewState: detail?.translation_review_state ?? (reply ? "pending" : "not_required"),
  };
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatDue(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function isOverdue(item: Inquiry): boolean {
  if (!item.apiCase) return false;
  return item.apiCase.is_overdue || (item.apiCase.queue_state === "open" && Date.parse(item.apiCase.response_due_at) <= Date.now());
}

function queueStateLabel(state: InquiryQueueState): string {
  return ({ open: "待响应", overdue: "已逾期", responded: "已响应", nurture: "跟进中", closed: "已关闭" })[state];
}

function customerRoleLabel(role: string): string {
  return ({ end_customer: "终端客户", representative: "采购代表", distributor: "经销商", unknown: "未识别" })[role] ?? role;
}

function translationStateLabel(state: string | undefined): string {
  return ({ not_required: "无需复核", pending: "待复核", approved: "已通过", rejected: "已驳回" })[state ?? ""] ?? "未生成回复";
}

function screeningChecks(item: Inquiry | undefined): QualificationCheckView[] {
  return item?.qualificationChecks ?? [];
}

function screeningResult(check: QualificationCheckView): QualificationCheckResult {
  const result = check.effective_result ?? check.decision?.result ?? check.result;
  return result === "clear" || result === "potential_match" || result === "blocked" || result === "unverified"
    ? result
    : "unverified";
}

function screeningOverallResult(item: Inquiry | undefined): QualificationCheckResult {
  const results = screeningChecks(item).map(screeningResult);
  if (results.includes("blocked")) return "blocked";
  if (results.includes("potential_match")) return "potential_match";
  if (results.includes("unverified") || !results.length) return "unverified";
  return "clear";
}

function screeningResultLabel(result: QualificationCheckResult): string {
  return ({
    clear: "已核验",
    potential_match: "疑似命中 · 待审核",
    blocked: "已阻断",
    unverified: "未核验 · 待补证",
  })[result];
}

function screeningBadgeStatus(result: QualificationCheckResult): string {
  return result === "clear" ? "approved" : result === "unverified" ? "waiting" : "rejected";
}

function screeningSummaryLabel(item: Inquiry | undefined): string {
  if (!item?.qualificationChecksLoaded) return "尚未读取服务端记录";
  const checks = screeningChecks(item);
  return checks.length
    ? `${checks.length} 条记录 · ${screeningResultLabel(screeningOverallResult(item))}`
    : "无记录 · 未核验";
}

function screeningReviewer(check: QualificationCheckView): string {
  return check.decision?.reviewer_id ?? check.reviewer_id ?? "未审核";
}

function screeningNotes(check: QualificationCheckView): string {
  return check.decision?.notes || check.notes || "无备注";
}

function formatAttachmentSize(value: number | null): string {
  if (value === null) return "大小未知";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadQueue() {
  if (!apiConfig.enabled) return;
  if (!apiConfig.usesTrustedHeaders) {
    sourceMessage.value = "当前没有受信身份，以下为本地演示队列；不会读取或写入服务端询盘。";
    return;
  }
  try {
    const result = await listInquiryQueue();
    inquiries.value = result.items.map((item) => mapInquiry(item));
    apiQueueLoaded.value = true;
    sourceMessage.value = "已连接服务端询盘队列。客户名称与地区字段尚未随 SLA 数据提供。";
    selectedId.value = inquiries.value[0]?.id ?? "";
    if (selectedId.value) await refreshSelected();
  } catch (error) {
    sourceTone.value = "warning";
    sourceMessage.value = `服务端队列读取失败，当前显示本地演示数据：${error instanceof Error ? error.message : "请求失败"}`;
  }
}

async function decideTranslation(decision: "approved" | "rejected") {
  const inquiry = selected.value;
  if (!canReviewInquiryTranslation(inquiry) || !inquiry.currentReply) return;
  actionMessage.value = "";
  refreshing.value = true;
  try {
    await reviewInquiryTranslation(inquiry.id, inquiry.currentReply.reply_revision_id, {
      content_hash: inquiry.currentReply.content_hash,
      decision,
      reason: reviewReason.value.trim() || null,
    });
    actionTone.value = "info";
    actionMessage.value = decision === "approved" ? "翻译复核已通过。" : "翻译复核已驳回。";
    reviewReason.value = "";
    await refreshSelected();
  } catch (error) {
    actionTone.value = "warning";
    actionMessage.value = error instanceof Error ? error.message : "提交翻译复核失败。";
  } finally {
    refreshing.value = false;
  }
}

function openReview() {
  router.push({ path: "/review", query: { quote: selected.value?.quote } });
}

function createDemoInquiry() {
  if (!intakeText.value.trim()) return;
  const next = inquiries.value.length + 1050;
  const created: Inquiry = {
    id: `RFQ-2026-${next}`,
    customer: "New demo customer",
    country: "Unspecified",
    subject: intakeText.value.trim().slice(0, 58),
    received: "刚刚",
    owner: "Liu Wei",
    status: "waiting_input",
    quote: `QT-${next}`,
    revision: "v1",
    runId: `run-demo-${next}`,
  };
  inquiries.value.unshift(created);
  selectedId.value = created.id;
  showIntake.value = false;
  intakeText.value = "";
  intakeMessage.value = "已添加到本地演示队列。接入 POST /rfqs 后将由服务端持久化。";
}

onMounted(() => {
  if (apiConfig.enabled && apiConfig.usesTrustedHeaders) void loadQueue();
  else if (apiConfig.enabled) sourceMessage.value = "当前没有受信身份，以下为本地演示队列；不会读取或写入服务端询盘。";
  else void refreshSelected();
});
</script>

<template>
  <PageHeader eyebrow="SALES WORKSPACE" title="询盘工作台" description="从询盘事实、报价版本到人工审核，集中处理一条可追溯的业务主线。">
    <template #actions>
      <button class="button button-primary" @click="showIntake = true"><FilePlus2 :size="16" /> 新建演示询盘</button>
    </template>
  </PageHeader>

  <Callout v-if="intakeMessage" title="演示数据已更新" :detail="intakeMessage" tone="info" />
  <Callout v-if="sourceMessage" :title="apiQueueLoaded ? '服务端询盘' : '演示模式'" :detail="sourceMessage" :tone="sourceTone" />
  <Callout v-if="actionMessage" :title="actionTone === 'warning' ? '操作未完成' : '操作结果'" :detail="actionMessage" :tone="actionTone" />

  <div class="workspace-grid">
    <section class="panel inquiry-panel">
      <div class="panel-toolbar">
        <div class="search-field"><Search :size="16" /><input v-model="search" aria-label="搜索询盘" placeholder="搜索客户、询盘编号或主题" /></div>
        <button class="icon-button" title="筛选"><Filter :size="17" /></button>
      </div>
      <div class="filter-tabs" role="tablist" aria-label="询盘状态筛选">
        <button v-for="tab in filterTabs" :key="tab[0]" class="filter-tab" :class="{ active: filter === tab[0] }" @click="filter = tab[0]">{{ tab[1] }}</button>
      </div>
      <div class="inquiry-list">
        <button v-for="item in filteredInquiries" :key="item.id" class="inquiry-row" :class="{ selected: selected?.id === item.id }" @click="selectedId = item.id">
          <span class="customer-avatar">{{ item.customer.slice(0, 2).toUpperCase() }}</span>
          <span class="inquiry-main"><strong>{{ item.customer }}</strong><span>{{ item.subject }}</span><small>{{ item.id }} · {{ item.received }}<template v-if="item.apiCase"> · {{ item.apiCase.original_language }}</template></small></span>
          <span class="inquiry-side"><StatusBadge :status="item.status" :label="item.apiCase ? queueStateLabel(item.apiCase.queue_state) : undefined" /><small v-if="item.apiCase" :class="{ 'sla-overdue-text': isOverdue(item) }">{{ isOverdue(item) ? '逾期' : '截止' }} {{ formatDue(item.apiCase.response_due_at) }}</small><small v-else>{{ item.revision }}</small></span>
        </button>
        <div v-if="!filteredInquiries.length" class="empty-state"><Search :size="20" /><strong>没有匹配的询盘</strong><span>调整搜索条件后再试。</span></div>
      </div>
    </section>

    <section class="panel inquiry-detail">
      <div v-if="selected" class="detail-heading"><div><div class="eyebrow">INQUIRY {{ selected.id }}</div><h2>{{ selected.customer }}</h2><p>{{ selected.country }} · 负责人 {{ selected.owner }}</p></div><StatusBadge :status="selected.status" :label="selected.apiCase ? queueStateLabel(selected.apiCase.queue_state) : undefined" /></div>
      <div class="detail-actions"><button class="button button-secondary" :disabled="refreshing" @click="refreshSelected"><RefreshCw :size="15" :class="{ spin: refreshing }" /> {{ selected?.apiCase ? '刷新询盘' : '刷新 Run' }}</button><button v-if="!selected?.apiCase" class="button button-primary" @click="openReview">查看版本 <ArrowRight :size="15" /></button></div>
      <div v-if="selected?.apiCase" class="sla-strip" :class="{ overdue: isOverdue(selected) }"><div><span class="label">首次响应截止</span><strong>{{ formatDue(selected.apiCase.response_due_at) }}</strong></div><StatusBadge :status="isOverdue(selected) ? 'rejected' : 'waiting'" :label="isOverdue(selected) ? '已逾期' : 'SLA 计时中'" /></div>
      <div class="subject-block"><span class="label">{{ selected?.apiCase ? '关联 RFQ' : '询盘主题' }}</span><strong>{{ selected?.subject }}</strong><small>{{ selected?.apiCase ? `来源 ${selected.apiCase.source_channel} · 客户角色 ${customerRoleLabel(selected.apiCase.customer_role)} · 原始语言 ${selected.apiCase.original_language}` : '原文和附件存储在受控业务存储中，界面只展示必要摘要。' }}</small><div v-if="selected?.apiCase?.attachments?.length" class="attachment-list" aria-label="询盘附件"><div class="attachment-list-heading"><Paperclip :size="14" /><span>询盘附件</span><small>{{ selected.apiCase.attachments.length }} 个</small></div><div v-for="attachment in selected.apiCase.attachments" :key="attachment.attachment_id" class="attachment-row"><div class="attachment-name"><strong>{{ attachment.filename }}</strong><small>{{ attachment.media_type }} · {{ formatAttachmentSize(attachment.size_bytes) }}</small></div><code :title="attachment.content_sha256">{{ attachment.storage_ref }} · {{ attachment.content_sha256.slice(0, 12) }}…</code></div></div></div>
      <div class="facts-grid"><div><span class="label">{{ selected?.apiCase ? 'RFQ 编号' : '当前报价' }}</span><strong>{{ selected?.apiCase?.rfq_id ?? selected?.quote }}</strong></div><div><span class="label">{{ selected?.apiCase ? '回复版本' : '当前版本' }}</span><strong>{{ selected?.apiCase ? selected.revision : selected?.revision }}</strong></div><div><span class="label">Run 状态</span><strong>{{ selected?.run?.status ?? (selected?.apiCase ? '未关联' : 'demo_waiting') }}</strong></div><div><span class="label">事件序号</span><strong>{{ selected?.run?.event_seq ?? "—" }}</strong></div></div>
      <section class="screening-section">
        <div class="section-title"><span class="screening-title"><ShieldCheck :size="15" /> 客户资质与制裁筛查</span><small>{{ screeningSummaryLabel(selected) }}</small></div>
        <div v-if="selected?.qualificationChecksError" class="screening-alert screening-alert-warning"><ShieldAlert :size="16" /><span>服务端筛查记录读取失败：{{ selected.qualificationChecksError }}。读取失败不代表已通过。</span></div>
        <div v-else-if="selected?.qualificationChecksLoaded && selected.qualificationChecks?.length" class="screening-list">
          <article v-for="check in selected.qualificationChecks" :key="check.check_id" class="screening-card" :class="`screening-result-${screeningResult(check)}`">
            <div class="screening-card-head"><div class="screening-source"><span class="screening-icon"><ShieldAlert v-if="screeningResult(check) !== 'clear'" :size="15" /><ShieldCheck v-else :size="15" /></span><div><strong>{{ check.source || '未注明来源' }}</strong><small>{{ check.reference || '未提供参考编号' }}</small></div></div><StatusBadge :status="screeningBadgeStatus(screeningResult(check))" :label="screeningResultLabel(screeningResult(check))" /></div>
            <dl class="screening-meta"><div><dt>检查时间</dt><dd>{{ formatDate(check.checked_at) }}</dd></div><div><dt>审核人</dt><dd>{{ screeningReviewer(check) }}</dd></div><div><dt>证据摘要</dt><dd><code>{{ check.evidence_hash || '未提供' }}</code></dd></div><div v-if="check.evidence_attachment_ref"><dt>附件引用</dt><dd>{{ check.evidence_attachment_ref }}</dd></div></dl>
            <p class="screening-notes"><span class="label">备注</span>{{ screeningNotes(check) }}</p>
          </article>
        </div>
        <div v-else-if="selected?.qualificationChecksLoaded" class="screening-empty"><ShieldAlert :size="17" /><span>尚无已记录的筛查结果。该状态不代表通过，报价和出运前仍需完成核验。</span></div>
        <div v-else class="screening-empty"><ShieldAlert :size="17" /><span>当前为演示数据或尚未连接受信 API，不读取也不推断外部筛查结果。</span></div>
      </section>
      <div v-if="selected?.apiCase" class="reply-section"><div class="section-title"><span>客户回复</span><StatusBadge :status="selected.translationReviewState ?? 'not_required'" :label="translationStateLabel(selected.translationReviewState)" /></div><template v-if="selected.currentReply"><div class="reply-language">{{ selected.currentReply.source_language }} → {{ selected.currentReply.target_language }} · R{{ selected.currentReply.revision_no }}</div><div class="reply-copy"><span class="label">源文</span><p>{{ selected.currentReply.source_content }}</p></div><div v-if="selected.currentReply.translated_content" class="reply-copy"><span class="label">译文</span><p>{{ selected.currentReply.translated_content }}</p></div><div v-if="selected.currentReply.attachments?.length" class="attachment-list reply-attachments" aria-label="回复附件"><div class="attachment-list-heading"><Paperclip :size="14" /><span>回复附件</span><small>{{ selected.currentReply.attachments.length }} 个</small></div><div v-for="attachment in selected.currentReply.attachments" :key="attachment.attachment_id" class="attachment-row"><div class="attachment-name"><strong>{{ attachment.filename }}</strong><small>{{ attachment.media_type }} · {{ formatAttachmentSize(attachment.size_bytes) }}</small></div><code :title="attachment.content_sha256">{{ attachment.storage_ref }} · {{ attachment.content_sha256.slice(0, 12) }}…</code></div></div><div v-if="selected.translationReview" class="review-stamp">{{ selected.translationReview.decision === 'approved' ? '复核通过' : '复核驳回' }} · {{ selected.translationReview.actor_id }} · {{ formatDate(selected.translationReview.reviewed_at) }}<span v-if="selected.translationReview.reason"> · {{ selected.translationReview.reason }}</span></div><div v-if="canReviewInquiryTranslation(selected)" class="translation-actions"><label class="label" :for="`review-reason-${selected.id}`">复核备注（可选）</label><textarea :id="`review-reason-${selected.id}`" v-model="reviewReason" rows="2" maxlength="2000" placeholder="记录翻译术语或客户表达的修订意见"></textarea><div><button class="button button-secondary" :disabled="refreshing" @click="decideTranslation('rejected')">驳回译文</button><button class="button button-primary" :disabled="refreshing" @click="decideTranslation('approved')">通过复核</button></div></div></template><p v-else class="reply-empty">尚无回复版本。</p></div>
      <div class="section-divider" />
      <div class="detail-section"><div class="section-title"><span>处理轨迹</span><small v-if="selected?.events?.length">{{ selected.events.length }} 个事件</small></div><div v-if="selected?.events?.length" class="timeline"><div v-for="event in selected.events" :key="event.event_id" class="timeline-item"><span class="timeline-dot" /><div><strong>{{ event.event_type }}</strong><small>{{ event.node_name ?? "workflow" }} · {{ new Date(event.occurred_at).toLocaleString("zh-CN") }}</small></div></div></div><div v-else class="timeline-empty"><span class="timeline-dot" /><span>演示 Run 尚未连接 API 事件流</span></div></div>
      <div class="detail-section"><div class="section-title"><span>下一步</span></div><div class="next-step"><span class="next-step-number">01</span><div><strong>{{ selected?.status === "needs_review" ? "核对报价阻断项" : "补充缺失事实" }}</strong><small>{{ selected?.status === "needs_review" ? "审核人需要确认版本差异和交付证据。" : "保存澄清后，服务端会创建新的 RFQ revision。" }}</small></div><ArrowRight :size="16" class="muted-icon" /></div></div>
    </section>
  </div>

  <div v-if="showIntake" class="modal-backdrop" @click.self="showIntake = false"><div class="modal"><div class="modal-heading"><div><div class="eyebrow">LOCAL DEMO</div><h2>新建演示询盘</h2></div><button class="icon-button" title="关闭" @click="showIntake = false">×</button></div><label class="field-label" for="intake">粘贴一段客户询盘</label><textarea id="intake" v-model="intakeText" rows="5" placeholder="例如：Please quote 5,000 pcs of M8 stainless bolts for Q4 delivery."></textarea><p class="form-note">此操作只更新浏览器内的演示队列，不会调用后端或发送邮件。</p><div class="modal-actions"><button class="button button-secondary" @click="showIntake = false">取消</button><button class="button button-primary" :disabled="!intakeText.trim()" @click="createDemoInquiry">加入队列</button></div></div></div>
</template>
