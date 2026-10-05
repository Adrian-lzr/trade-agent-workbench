<script setup lang="ts">
import { computed, ref } from "vue";
import { ArrowRight, Check, Clock3, LockKeyhole, MessageSquareText, ShieldAlert, X } from "lucide-vue-next";
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
import type { QuoteRevision } from "../types";

const router = useRouter();

type QueueItem = {
  revision: QuoteRevision;
  customer: string;
  age: string;
  owner: string;
  blockers: number;
};

type DecisionRecord = {
  decision: "approved" | "rejected";
  approvalId: string;
  replay: boolean;
};

const queue: QueueItem[] = [
  { revision: quoteRevisions[0], customer: "Northwind Industrial", age: "刚刚", owner: "Liu Wei", blockers: 1 },
  { revision: { ...quoteRevisions[0], id: "qrev-1041-v1", quoteId: "QT-1041", version: "v1", amount: "1,280.00", blockers: [] }, customer: "Sakura Motion", age: "42 分钟", owner: "Chen Jia", blockers: 0 },
  { revision: { ...quoteRevisions[0], id: "qrev-1036-v4", quoteId: "QT-1036", version: "v4", amount: "860.00", blockers: [] }, customer: "Atlas Fabrication", age: "昨天", owner: "Liu Wei", blockers: 0 },
];

const decisionLoading = ref<string | null>(null);
const decisions = ref<Record<string, DecisionRecord>>({});
const queueError = ref("");
const queueSuccess = ref("");

const calloutTone = computed<"info" | "warning" | "danger">(() => {
  if (queueError.value) return "danger";
  if (queueSuccess.value) return "info";
  return "info";
});

const calloutTitle = computed(() => {
  if (queueError.value) return "审批请求未完成";
  if (queueSuccess.value) return "审批已记录";
  return apiConfig.enabled ? "审批 API 已接入，队列为演示样本" : "演示审批队列";
});

const calloutDetail = computed(() => {
  if (queueError.value) return queueError.value;
  if (queueSuccess.value) return `${queueSuccess.value}。队列统计不会被改写为服务端汇总。`;
  if (!apiConfig.enabled) return "当前队列和统计数字是本地演示数据。审批按钮保持锁定，避免在没有服务端身份时伪造决定。";
  if (!apiConfig.usesTrustedHeaders) return "当前队列仍是演示样本。API 模式需要部署提供的受信身份，浏览器不能自行切换角色。";
  return "队列条目仍是演示样本；只有同时匹配配置的 quotation、revision、content hash、审批角色且没有 blocking 项时，才允许提交服务端批准。";
});

function hasBlocking(item: QueueItem): boolean {
  return item.revision.blockers.some((blocker) => blocker.severity === "blocking");
}

function canApprove(item: QueueItem): boolean {
  return !hasBlocking(item) && canSubmitQuoteDecision(item.revision);
}

function lockReason(item: QueueItem): string {
  if (hasBlocking(item)) return `存在 ${item.revision.blockers.filter((blocker) => blocker.severity === "blocking").length} 个 blocking 项，需先处理。`;
  if (!apiConfig.enabled) return "演示模式不会写入审批结果。";
  return quoteTargetStatus(item.revision);
}

function itemStatus(item: QueueItem): string {
  return decisions.value[item.revision.id]?.decision ?? item.revision.status;
}

async function approve(item: QueueItem) {
  if (!canApprove(item) || decisionLoading.value) return;
  decisionLoading.value = item.revision.id;
  queueError.value = "";
  queueSuccess.value = "";
  try {
    const result = await submitQuoteDecision(item.revision, "approved", null);
    decisions.value[item.revision.id] = {
      decision: result.decision,
      approvalId: result.approval_id,
      replay: result.idempotent_replay,
    };
    queueSuccess.value = `版本 ${item.revision.version} 已批准（approval ${result.approval_id}${result.idempotent_replay ? "，幂等重放" : ""}）`;
  } catch (error) {
    queueError.value = error instanceof ApiError ? error.message : "审批请求失败，请稍后重试。";
  } finally {
    decisionLoading.value = null;
  }
}
</script>

<template>
  <PageHeader eyebrow="HUMAN DECISIONS" title="审批队列" description="只对具体 revision 做决定。审批结果必须由服务端事务绑定版本摘要并记录审计事件。">
    <template #actions><span class="role-lock"><LockKeyhole :size="14" /> Reviewer only</span></template>
  </PageHeader>

  <Callout :tone="calloutTone" :title="calloutTitle" :detail="calloutDetail" />

  <div class="approval-overview"><div class="approval-stat"><span class="stat-icon amber"><Clock3 :size="18" /></span><div><strong>3</strong><span>演示：等待审核</span></div></div><div class="approval-stat"><span class="stat-icon red"><ShieldAlert :size="18" /></span><div><strong>1</strong><span>演示：有阻断项</span></div></div><div class="approval-stat"><span class="stat-icon green"><Check :size="18" /></span><div><strong>8</strong><span>演示：本周已批准</span></div></div><div class="approval-stat"><span class="stat-icon slate"><MessageSquareText :size="18" /></span><div><strong>2</strong><span>演示：待业务员修改</span></div></div></div>

  <section class="panel queue-panel"><div class="queue-heading"><div><div class="eyebrow">REVIEW QUEUE</div><h2>待处理版本</h2></div><span class="queue-updated">演示统计 · 最后刷新：刚刚</span></div><div class="queue-list"><article v-for="item in queue" :key="item.revision.id" class="queue-item"><div class="queue-item-main"><div class="queue-item-top"><span class="quote-id">{{ item.revision.quoteId }}</span><StatusBadge :status="itemStatus(item)" /><span class="queue-age">{{ item.age }}</span></div><h3>{{ item.customer }}</h3><p>{{ item.revision.items[0]?.description }} · {{ item.revision.amount }} {{ item.revision.currency }}</p><div class="queue-meta"><span>版本 {{ item.revision.version }}</span><span>业务员 {{ item.owner }}</span><span v-if="item.blockers" class="meta-danger"><ShieldAlert :size="13" /> {{ item.blockers }} 个阻断项</span><span v-else class="meta-ok"><Check :size="13" /> 质量门通过</span></div></div><div class="queue-actions"><button class="button button-secondary" @click="router.push('/review')">查看差异 <ArrowRight :size="15" /></button><button class="icon-button" :title="canApprove(item) ? (decisionLoading === item.revision.id ? '提交中' : '批准此版本') : lockReason(item)" :disabled="!canApprove(item) || decisionLoading !== null" @click="approve(item)"><Check :size="16" :class="{ spin: decisionLoading === item.revision.id }" /></button><button class="icon-button danger-icon" title="退回请在版本审核中操作" disabled><X :size="16" /></button></div></article></div></section>
</template>
