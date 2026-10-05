<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { BarChart3, CalendarDays, Info, LockKeyhole, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-vue-next";
import PageHeader from "../components/PageHeader.vue";
import { metrics as demoMetrics } from "../data/demo";
import Callout from "../components/Callout.vue";
import { ApiError, apiConfig, queryAnalytics } from "../services/api";
import type { AnalyticsMetricName, AnalyticsResult, MetricCard } from "../types";

type AnalyticsState = "demo" | "loading" | "api-fake" | "api-real" | "fallback";
type CalloutTone = "info" | "warning" | "danger";
type DefinitionRow = { name: string; definition: string; source?: string };
type ChartBar = { height: number; label: string; value: number };

const demoBars = [42, 55, 48, 68, 61, 78, 72, 91, 84, 96, 82, 88];
const demoChartLabels = ["W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "W10", "W11", "W12"];
const metricOrder: AnalyticsMetricName[] = ["rfq_count", "quote_revision_count", "run_success_rate"];
const metricLabels: Record<AnalyticsMetricName, string> = {
  rfq_count: "询盘数量",
  quote_revision_count: "报价修订数",
  run_success_rate: "运行成功率",
};
const demoDefinitions: DefinitionRow[] = [
  { name: "待审核报价", definition: "当前有效版本状态为 in_review 的报价数量。" },
  { name: "首份草稿中位时长", definition: "询盘创建到第一份可审核草稿的中位耗时。" },
  { name: "报价阻断率", definition: "带至少一个 blocking quality-gate 的可审核草稿占比。" },
  { name: "已批准金额", definition: "按当前有效已批准版本汇总，不重复计算历史修订。" },
];

const liveQueriesEnabled = apiConfig.enabled && apiConfig.usesTrustedHeaders;
const analyticsState = ref<AnalyticsState>(liveQueriesEnabled ? "loading" : "demo");
const analyticsResults = ref<Partial<Record<AnalyticsMetricName, AnalyticsResult>>>({});
const analyticsError = ref("");
const isLoading = computed(() => analyticsState.value === "loading");
const hasApiResults = computed(() => analyticsState.value === "api-fake" || analyticsState.value === "api-real");

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "分析 API 请求失败，请稍后重试。";
}

async function loadAnalytics() {
  if (!liveQueriesEnabled) {
    analyticsState.value = "demo";
    analyticsResults.value = {};
    analyticsError.value = "";
    return;
  }

  analyticsState.value = "loading";
  analyticsError.value = "";
  try {
    const entries = await Promise.all(
      metricOrder.map(async (metric) => {
        const dimensions = metric === "quote_revision_count" ? ["event_date"] : [];
        return [metric, await queryAnalytics(metric, dimensions)] as const;
      }),
    );
    const nextResults: Partial<Record<AnalyticsMetricName, AnalyticsResult>> = {};
    for (const [metric, result] of entries) nextResults[metric] = result;
    analyticsResults.value = nextResults;
    analyticsState.value = entries.some(([, result]) => result.mode === "fake" || result.is_synthetic)
      ? "api-fake"
      : "api-real";
  } catch (error) {
    analyticsResults.value = {};
    analyticsState.value = "fallback";
    analyticsError.value = errorMessage(error);
  }
}

onMounted(loadAnalytics);

const statusTone = computed<CalloutTone>(() => {
  if (analyticsState.value === "fallback") return "danger";
  if (analyticsState.value === "api-fake" || analyticsState.value === "demo") return "warning";
  return "info";
});

const statusTitle = computed(() => {
  switch (analyticsState.value) {
    case "loading":
      return "正在读取授权分析视图";
    case "api-real":
      return "已连接授权分析视图";
    case "api-fake":
      return "后端返回演示分析结果";
    case "fallback":
      return "API 请求失败，已回退演示数据";
    default:
      return apiConfig.enabled ? "等待受信身份，当前使用演示数据" : "当前使用本地演示数据";
  }
});

const statusDetail = computed(() => {
  switch (analyticsState.value) {
    case "loading":
      return "正在查询 rfq_count、quote_revision_count 和 run_success_rate。页面不会在浏览器内生成指标。";
    case "api-real":
      return "三个指标均来自服务端授权视图，结果带有 metric definition 和 source view。";
    case "api-fake":
      return "请求已到达后端，但当前后端模式为 fake，结果标记为 is_synthetic=true，不代表生产事实。";
    case "fallback":
      return `${analyticsError.value} 页面暂时显示本地演示指标，不能作为服务端事实。`;
    default:
      return apiConfig.enabled
        ? "API 模式需要部署提供的受信组织、操作者和角色身份；身份未配置时不会发起查询。"
        : "未启用 API 请求。配置 VITE_API_MODE=api 及受信身份后，页面会查询三个固定指标。";
  }
});

function metricValue(metric: AnalyticsMetricName): number | null {
  const points = analyticsResults.value[metric]?.points ?? [];
  if (!points.length) return null;
  if (metric === "quote_revision_count" && points.length > 1) {
    const total = points.reduce((sum, point) => sum + point.value, 0);
    return Number.isFinite(total) ? total : null;
  }
  const value = points[0]?.value;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatMetricValue(metric: AnalyticsMetricName): string {
  const value = metricValue(metric);
  if (value === null) return "—";
  if (metric === "run_success_rate") return `${value.toFixed(1)}%`;
  return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(1);
}

const displayedMetrics = computed<MetricCard[]>(() => {
  if (isLoading.value) {
    return metricOrder.map((metric) => ({
      label: metricLabels[metric],
      value: "…",
      change: "读取中",
      trend: "flat",
      note: "等待服务端",
    }));
  }
  if (!hasApiResults.value) return demoMetrics;
  return metricOrder.map((metric) => ({
    label: metricLabels[metric],
    value: formatMetricValue(metric),
    change: "服务端",
    trend: "flat",
    note: analyticsState.value === "api-fake" ? "fake / synthetic" : "授权视图",
  }));
});

const chartBars = computed<ChartBar[]>(() => {
  if (isLoading.value) return [];
  if (!hasApiResults.value) {
    return demoBars.map((value, index) => ({ height: value, label: demoChartLabels[index], value }));
  }
  const points = analyticsResults.value.quote_revision_count?.points ?? [];
  if (!points.length) return [];
  const visiblePoints = points.slice(-12);
  const max = Math.max(...visiblePoints.map((point) => point.value), 1);
  return visiblePoints.map((point, index) => ({
    height: Math.max(4, (point.value / max) * 100),
    label: String(point.dimensions.event_date ?? `P${index + 1}`),
    value: point.value,
  }));
});

const chartTitle = computed(() => (hasApiResults.value ? "按日报价修订" : "每周可审核草稿"));
const chartLegend = computed(() => (hasApiResults.value ? "报价修订数" : "可审核草稿"));

const displayedDefinitions = computed<DefinitionRow[]>(() => {
  if (isLoading.value) return [];
  if (!hasApiResults.value) return demoDefinitions;
  return metricOrder.flatMap((metric) => {
    const result = analyticsResults.value[metric];
    if (!result) return [];
    return [{
      name: metricLabels[metric],
      definition: `${result.definition.definition} 公式：${result.definition.formula}`,
      source: result.definition.source_view,
    }];
  });
});
</script>

<template>
  <PageHeader eyebrow="READ-ONLY OPERATIONS" title="经营指标" description="用固定口径查看处理量、时长和质量门结果。这里不允许自然语言写入或修改业务事实。">
    <template #actions>
      <div class="page-header-actions">
        <div class="date-filter"><CalendarDays :size="15" /><span>过去 30 天</span><span class="chevron">⌄</span></div>
        <button class="icon-button" title="刷新分析" :disabled="isLoading || !liveQueriesEnabled" @click="loadAnalytics">
          <RefreshCw :size="15" :class="{ spin: isLoading }" />
        </button>
      </div>
    </template>
  </PageHeader>

  <Callout :tone="statusTone" :title="statusTitle" :detail="statusDetail" />

  <div class="metric-grid" :class="{ 'metric-grid-api': hasApiResults }">
    <article v-for="metric in displayedMetrics" :key="metric.label" class="metric-card">
      <div class="metric-top"><span>{{ metric.label }}</span><Info :size="15" class="muted-icon" /></div>
      <strong>{{ metric.value }}</strong>
      <div class="metric-change" :class="metric.trend === 'up' ? 'change-up' : metric.trend === 'down' ? 'change-down' : 'change-flat'">
        <TrendingUp v-if="metric.trend === 'up'" :size="14" />
        <TrendingDown v-else-if="metric.trend === 'down'" :size="14" />
        <Minus v-else :size="14" />
        {{ metric.change }} <small>{{ metric.note }}</small>
      </div>
    </article>
  </div>

  <div class="analytics-grid">
    <section class="panel chart-panel">
      <div class="chart-heading">
        <div><div class="eyebrow">THROUGHPUT</div><h2>{{ chartTitle }}</h2></div>
        <span class="chart-legend"><i /> {{ chartLegend }}</span>
      </div>
      <div v-if="chartBars.length" class="chart">
        <div class="chart-y"><span>100</span><span>75</span><span>50</span><span>25</span><span>0</span></div>
        <div class="chart-area">
          <div class="chart-gridlines"><i /><i /><i /><i /><i /></div>
          <div class="bar-list"><span v-for="bar in chartBars" :key="bar.label" class="bar" :style="{ height: `${bar.height}%` }" :title="`${bar.value} 份`" /></div>
          <div class="chart-x"><span v-for="bar in chartBars" :key="`${bar.label}-label`">{{ bar.label }}</span></div>
        </div>
      </div>
      <div v-else class="empty-state"><strong>{{ isLoading ? "正在读取分析数据" : "暂无报价修订数据" }}</strong><span>{{ isLoading ? "等待服务端返回授权指标。" : "授权视图当前没有可展示的点。" }}</span></div>
    </section>

    <section class="panel definitions-panel">
      <div class="side-heading"><div><div class="eyebrow">METRIC CATALOG</div><h3>口径说明</h3></div><BarChart3 :size="17" class="muted-icon" /></div>
      <div v-if="displayedDefinitions.length" class="definitions-list">
        <div v-for="definition in displayedDefinitions" :key="definition.name">
          <strong>{{ definition.name }}</strong>
          <p>{{ definition.definition }}</p>
          <small v-if="definition.source" class="definition-source">来源：{{ definition.source }}</small>
        </div>
      </div>
      <div v-else class="empty-state"><strong>正在读取指标口径</strong><span>等待服务端返回 definition。</span></div>
      <div class="metric-footnote"><LockKeyhole :size="14" /> {{ hasApiResults ? (analyticsState === "api-fake" ? "后端结果已标记为 synthetic；请勿当作生产事实。" : "指标只访问授权视图，不暴露客户邮箱、电话或内部成本。") : isLoading ? "服务端查询进行中，暂不展示本地口径。" : "当前为演示指标；API 请求成功后会展示服务端 definition 和 source view。" }}</div>
    </section>
  </div>
</template>
