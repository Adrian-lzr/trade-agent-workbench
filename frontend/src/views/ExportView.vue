<script setup lang="ts">
import { computed, ref } from "vue";
import { Download, FileCheck2, FileOutput, LockKeyhole, RefreshCw, ShieldAlert } from "lucide-vue-next";
import PageHeader from "../components/PageHeader.vue";
import StatusBadge from "../components/StatusBadge.vue";
import Callout from "../components/Callout.vue";
import { quoteRevisions } from "../data/demo";
import {
  ApiError,
  apiConfig,
  canCreateQuoteArtifact,
  createQuoteArtifact,
  downloadQuoteArtifact,
  quoteTargetStatus,
} from "../services/api";

type ArtifactRow = {
  id: string;
  quote: string;
  revision: string;
  customer: string;
  format: string;
  status: "blocked" | "ready" | "pending";
  created: string;
  reason: string;
  downloadable: boolean;
};

const artifacts = ref<ArtifactRow[]>([
  { id: "artifact-demo-1048", quote: "QT-1048", revision: "v3", customer: "Northwind Industrial", format: "PDF · Pro forma invoice", status: "blocked", created: "—", reason: "等待报价批准", downloadable: false },
  { id: "artifact-demo-1036", quote: "QT-1036", revision: "v4", customer: "Atlas Fabrication", format: "PDF · Quotation", status: "ready", created: "2026-09-30 16:10", reason: "演示文件，不可下载", downloadable: false },
  { id: "artifact-demo-1041", quote: "QT-1041", revision: "v1", customer: "Sakura Motion", format: "PDF · Quotation", status: "pending", created: "刚刚", reason: "正在渲染固定模板", downloadable: false },
]);
const selectedRevision = quoteRevisions[0];
const creating = ref(false);
const downloading = ref<string | null>(null);
const apiMessage = ref("");
const apiState = ref<"idle" | "success" | "error">("idle");
const canCreate = computed(() => canCreateQuoteArtifact(selectedRevision));

const calloutTitle = computed(() => {
  if (apiState.value === "success") return "文件已由服务端生成";
  if (apiState.value === "error") return "文件请求未完成";
  if (!apiConfig.enabled) return "演示数据未提供正式下载";
  return canCreate.value ? "可从已批准快照生成文件" : "当前版本尚未满足导出条件";
});
const calloutDetail = computed(() => {
  if (apiState.value !== "idle") return apiMessage.value;
  if (!apiConfig.enabled) return "当前页面展示演示 Artifact 状态。API 模式并配置完整报价目标后，生成按钮才会调用服务端。";
  return canCreate.value ? "点击生成文件后，服务端会校验批准记录、content hash 和固定模板版本。" : quoteTargetStatus(selectedRevision);
});

async function createArtifact() {
  if (!canCreate.value || creating.value) return;
  creating.value = true;
  apiState.value = "idle";
  try {
    const result = await createQuoteArtifact(selectedRevision);
    artifacts.value = [
      {
        id: result.artifact_id,
        quote: result.quotation_id,
        revision: selectedRevision.version,
        customer: "Northwind Industrial",
        format: "PDF · Pro forma invoice",
        status: result.status === "ready" ? "ready" : "pending",
        created: "刚刚",
        reason: result.status === "ready" ? "服务端固定模板" : "等待服务端完成渲染",
        downloadable: result.status === "ready",
      },
      ...artifacts.value.filter((item) => item.id !== result.artifact_id),
    ];
    apiState.value = "success";
    apiMessage.value = `Artifact ${result.artifact_id} 已生成，金额 ${result.amount_total} USD；摘要 ${result.content_sha256 ?? "未返回"}。`;
  } catch (error) {
    apiState.value = "error";
    apiMessage.value = error instanceof ApiError ? error.message : "文件生成失败，请稍后重试。";
  } finally {
    creating.value = false;
  }
}

async function downloadArtifact(artifact: ArtifactRow) {
  if (!artifact.downloadable || downloading.value) return;
  downloading.value = artifact.id;
  try {
    const blob = await downloadQuoteArtifact(artifact.id);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `proforma-invoice-${artifact.quote}-${artifact.revision}.pdf`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    apiState.value = "error";
    apiMessage.value = error instanceof ApiError ? error.message : "文件下载失败，请稍后重试。";
  } finally {
    downloading.value = null;
  }
}
</script>

<template>
  <PageHeader eyebrow="CONTROLLED ARTIFACTS" title="文件导出" description="文件只能从已批准的不可变报价快照生成。渲染不会再次调用模型修改金额或承诺。">
    <template #actions><span class="role-lock"><LockKeyhole :size="14" /> 私有文件</span></template>
  </PageHeader>
  <Callout :tone="apiState === 'error' ? 'danger' : apiState === 'success' ? 'info' : 'warning'" :title="calloutTitle" :detail="calloutDetail" />
  <section class="panel export-panel"><div class="export-toolbar"><div class="export-heading"><FileOutput :size="19" /><div><h2>Artifacts</h2><p>固定模板版本 · `proforma-v1`</p></div></div><div class="export-toolbar-actions"><button class="button button-secondary" :disabled="!canCreate || creating" @click="createArtifact"><RefreshCw :size="15" :class="{ spin: creating }" /> {{ creating ? "生成中…" : "生成 Pro forma" }}</button></div></div><div class="artifact-table"><div class="artifact-row artifact-head"><span>报价 / 客户</span><span>类型</span><span>状态</span><span>创建时间</span><span /></div><div v-for="artifact in artifacts" :key="artifact.id" class="artifact-row"><div class="artifact-name"><strong>{{ artifact.quote }} · {{ artifact.revision }}</strong><span>{{ artifact.customer }}</span></div><span class="artifact-format">{{ artifact.format }}</span><StatusBadge :status="artifact.status === 'blocked' ? 'rejected' : artifact.status === 'ready' ? 'approved' : 'waiting'" :label="artifact.status === 'blocked' ? '不可导出' : artifact.status === 'ready' ? (artifact.downloadable ? '可下载' : '演示文件') : '生成中'" /><span class="artifact-date">{{ artifact.created }}</span><button v-if="artifact.status === 'ready' && artifact.downloadable" class="icon-button" :title="downloading === artifact.id ? '下载中' : '下载文件'" :disabled="downloading === artifact.id" @click="downloadArtifact(artifact)"><Download :size="16" :class="{ spin: downloading === artifact.id }" /></button><span v-else class="artifact-lock"><LockKeyhole :size="15" /></span><small v-if="artifact.reason" class="artifact-reason"><ShieldAlert :size="13" /> {{ artifact.reason }}</small></div></div></section>
  <section class="export-notes"><div><FileCheck2 :size="17" /><span><strong>文件一致性</strong><small>文件摘要必须能回溯到批准版本和模板版本。</small></span></div><div><ShieldAlert :size="17" /><span><strong>不自动发送</strong><small>导出完成不代表邮件已发送；外发需要单独授权能力。</small></span></div></section>
</template>
