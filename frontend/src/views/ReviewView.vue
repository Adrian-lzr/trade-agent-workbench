<script setup lang="ts">
import { computed, ref } from "vue";
import { ArrowLeft, Check, ChevronDown, Copy, GitCompareArrows, LockKeyhole, MessageSquareText, RotateCcw, ShieldAlert } from "lucide-vue-next";
import { useRouter } from "vue-router";
import PageHeader from "../components/PageHeader.vue";
import StatusBadge from "../components/StatusBadge.vue";
import Callout from "../components/Callout.vue";
import { quoteRevisions } from "../data/demo";
import {
  ApiError,
  apiConfig,
  canSubmitQuoteDecision,
  quoteTargetStatus,
  submitQuoteDecision,
} from "../services/api";

const router = useRouter();
const selectedVersion = ref("qrev-1048-v3");
const showDiff = ref(true);
const copied = ref(false);
const decisionLoading = ref(false);
const decisionState = ref<"idle" | "success" | "error">("idle");
const decisionMessage = ref("");
const approvalResult = ref<{ decision: "approved" | "rejected"; approvalId: string; replay: boolean } | null>(null);
const rejectionReason = ref("请补充阻断项后重新提交。");
const revision = computed(() => quoteRevisions.find((item) => item.id === selectedVersion.value) ?? quoteRevisions[0]);
const previous = computed(() => quoteRevisions.find((item) => item.id !== selectedVersion.value));
const canDecide = computed(() => canSubmitQuoteDecision(revision.value));
const displayedStatus = computed(() => approvalResult.value?.decision ?? revision.value.status);
const gateTitle = computed(() => {
  if (decisionState.value === "success") return approvalResult.value?.decision === "approved" ? "审批已记录" : "退回决定已记录";
  if (decisionState.value === "error") return "审批请求未完成";
  return revision.value.blockers.some((item) => item.severity === "blocking") ? "当前版本有 1 个阻断项" : "当前版本通过本地展示质量门";
});
const gateDetail = computed(() => {
  if (decisionState.value === "success") {
    return `服务端已返回 approval ${approvalResult.value?.approvalId}${approvalResult.value?.replay ? "（幂等重放）" : ""}。当前页面不会替代后端审计记录。`;
  }
  if (decisionState.value === "error") return decisionMessage.value;
  if (!apiConfig.enabled) return "当前是演示模式。审批按钮会在 API 模式、受信身份和完整版本摘要均配置后启用。";
  return `${quoteTargetStatus(revision.value)} 此页面不会在浏览器内伪造批准结果。`;
});

function copyHash() {
  copied.value = true;
  window.setTimeout(() => (copied.value = false), 1600);
}

async function decide(decision: "approved" | "rejected") {
  if (!canDecide.value || decisionLoading.value) return;
  decisionLoading.value = true;
  decisionState.value = "idle";
  try {
    const result = await submitQuoteDecision(
      revision.value,
      decision,
      decision === "rejected" ? rejectionReason.value.trim() || null : null,
    );
    approvalResult.value = {
      decision: result.decision,
      approvalId: result.approval_id,
      replay: result.idempotent_replay,
    };
    decisionState.value = "success";
  } catch (error) {
    decisionState.value = "error";
    decisionMessage.value = error instanceof ApiError ? error.message : "审批请求失败，请稍后重试。";
  } finally {
    decisionLoading.value = false;
  }
}
</script>

<template>
  <PageHeader eyebrow="REVIEW WORKSPACE" title="版本审核" description="审核具体报价版本，而不是给整份报价打一个永久标签。所有客户可见变化都必须重新审核。">
    <template #actions><button class="button button-secondary" @click="router.push('/inbox')"><ArrowLeft :size="15" /> 返回询盘</button></template>
  </PageHeader>

  <Callout :tone="decisionState === 'success' ? 'info' : decisionState === 'error' ? 'danger' : 'warning'" :title="gateTitle" :detail="gateDetail" />

  <div class="review-layout">
    <section class="panel review-main">
      <div class="review-heading"><div><div class="eyebrow">QUOTE QT-1048</div><h2>Northwind Industrial</h2><p>M8 stainless bolts for Q4 line · USD</p></div><StatusBadge :status="displayedStatus" /></div>
      <div class="version-toolbar"><div class="version-select"><GitCompareArrows :size="16" /><select v-model="selectedVersion" aria-label="选择报价版本"><option v-for="item in quoteRevisions" :key="item.id" :value="item.id">{{ item.version }} · {{ item.createdAt }} · {{ item.status }}</option></select><ChevronDown :size="15" /></div><button class="toggle-button" :class="{ active: showDiff }" @click="showDiff = !showDiff">{{ showDiff ? "隐藏差异" : "显示差异" }}</button></div>
      <div v-if="showDiff" class="diff-strip"><div class="diff-icon"><GitCompareArrows :size="18" /></div><div><strong>{{ revision.version }} 相比 {{ previous?.version }} 的版本变化</strong><span>报价快照仍为 {{ revision.amount }} {{ revision.currency }}；变更需绑定当前 content hash。</span></div><span class="diff-count">{{ revision.version === "v3" ? "1 项变更" : "历史版本" }}</span></div>
      <div class="quote-summary"><div class="summary-cell"><span class="label">报价总额</span><strong class="amount">{{ revision.amount }} <small>{{ revision.currency }}</small></strong></div><div class="summary-cell"><span class="label">有效期</span><strong>{{ revision.validity }}</strong></div><div class="summary-cell"><span class="label">报价版本</span><strong>{{ revision.version }}</strong></div><div class="summary-cell"><span class="label">创建人</span><strong>{{ revision.createdBy }}</strong></div></div>
      <div class="section-divider" />
      <div class="section-title"><span>客户可见明细</span><small>来自不可变快照</small></div>
      <div class="table-wrap"><table class="data-table"><thead><tr><th>SKU / 描述</th><th>数量</th><th>单价</th><th class="align-right">金额</th></tr></thead><tbody><tr v-for="item in revision.items" :key="item.sku"><td><strong>{{ item.sku }}</strong><span>{{ item.description }}</span></td><td>{{ item.quantity }} {{ item.unit }}</td><td>{{ item.unitPrice }} {{ revision.currency }}</td><td class="align-right"><strong>{{ item.total }} {{ revision.currency }}</strong></td></tr></tbody></table></div>
      <div class="section-divider" />
      <div class="section-title"><span>对客回复事实</span><small>草稿只能引用已确认事实</small></div>
      <div class="reply-preview"><div class="reply-meta"><span class="avatar avatar-small">LW</span><span><strong>Reply draft · English</strong><small>由 ModelGateway 生成，事实门已校验</small></span><MessageSquareText :size="17" class="muted-icon" /></div><p>Dear Northwind Industrial,</p><p>Thank you for your inquiry. We can quote <mark>5,000 pcs</mark> of M8 × 30 A2-70 stainless steel bolts at <mark>USD 0.08 / piece</mark>, for a total of <mark>USD 400.00</mark>.</p><p class="reply-muted">Delivery timing and certificate documentation remain subject to confirmation.</p></div>
    </section>

    <aside class="review-side">
      <section class="panel blocker-panel"><div class="side-heading"><div><div class="eyebrow">QUALITY GATE</div><h3>阻断项与警告</h3></div><span class="blocker-count">{{ revision.blockers.length }}</span></div><div class="blocker-list"><div v-for="blocker in revision.blockers" :key="blocker.code" class="blocker-item" :class="`severity-${blocker.severity}`"><ShieldAlert v-if="blocker.severity === 'blocking'" :size="17" /><span v-else class="warning-symbol">!</span><div><strong>{{ blocker.title }}</strong><p>{{ blocker.detail }}</p><small>{{ blocker.code }}</small></div></div><div v-if="!revision.blockers.length" class="empty-inline"><Check :size="17" /> 此版本没有记录的阻断项</div></div></section>
      <section class="panel audit-panel"><div class="side-heading"><div><div class="eyebrow">IMMUTABLE SNAPSHOT</div><h3>版本证据</h3></div><LockKeyhole :size="17" class="muted-icon" /></div><dl class="evidence-list"><div><dt>Revision ID</dt><dd>{{ revision.id }}</dd></div><div><dt>Content hash</dt><dd><code>{{ revision.contentHash }}</code><button class="icon-button icon-button-small" :title="copied ? '已复制' : '复制摘要'" @click="copyHash"><Copy :size="13" /></button></dd></div><div><dt>创建时间</dt><dd>{{ revision.createdAt }}</dd></div></dl><p class="audit-note">审批必须携带 expected revision 与 expected content hash。版本变化后，旧决定自动失效。</p></section>
      <section class="panel action-panel"><div class="side-heading"><div><div class="eyebrow">REVIEW ACTION</div><h3>审核决定</h3></div><LockKeyhole :size="16" class="muted-icon" /></div><label v-if="canDecide" class="decision-reason-label" for="rejection-reason">退回意见（可选）</label><textarea v-if="canDecide" id="rejection-reason" v-model="rejectionReason" class="decision-reason" rows="2" /><button class="button button-primary button-wide" :disabled="!canDecide || decisionLoading" @click="decide('approved')"><Check :size="16" /> {{ decisionLoading ? "提交中…" : "批准此版本" }}</button><button class="button button-secondary button-wide" :disabled="!canDecide || decisionLoading" @click="decide('rejected')"><RotateCcw :size="16" /> 退回并要求修改</button><small class="disabled-note">{{ canDecide ? "请求将携带当前 content hash 和幂等键，由服务端执行角色、版本与并发校验。" : quoteTargetStatus(revision) }}</small></section>
    </aside>
  </div>
</template>
