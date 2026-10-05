<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import {
  BarChart3,
  Bell,
  ChevronDown,
  ClipboardCheck,
  FileOutput,
  Inbox,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  Wifi,
} from "lucide-vue-next";
import { apiConfig, apiModeLabel } from "./services/api";

const route = useRoute();
const sidebarOpen = ref(false);
const collapsed = ref(false);

const navItems = [
  { to: "/inbox", label: "询盘工作台", hint: "RFQ 与报价", icon: Inbox },
  { to: "/review", label: "版本审核", hint: "事实与阻断", icon: ClipboardCheck },
  { to: "/approvals", label: "审批队列", hint: "人工决定", icon: ShieldCheck },
  { to: "/exports", label: "文件导出", hint: "批准后生成", icon: FileOutput },
  { to: "/analytics", label: "经营指标", hint: "只读查询", icon: BarChart3 },
];

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? "询盘工作台");
const roleLabel = computed(() => {
  if (apiConfig.usesTrustedHeaders) {
    if (apiConfig.role === "reviewer") return "审核人";
    if (apiConfig.role === "admin") return "管理员";
    return "业务员";
  }
  return "演示身份";
});
</script>

<template>
  <div class="app-frame">
    <div v-if="sidebarOpen" class="mobile-scrim" @click="sidebarOpen = false" />
    <aside class="sidebar" :class="{ 'is-open': sidebarOpen, 'is-collapsed': collapsed }">
      <div class="brand-row">
        <RouterLink to="/inbox" class="brand" @click="sidebarOpen = false">
          <span class="brand-mark">T</span>
          <span v-if="!collapsed" class="brand-copy">
            <strong>Trade Agent</strong>
            <small>Review workbench</small>
          </span>
        </RouterLink>
        <button class="icon-button sidebar-close" title="收起导航" @click="collapsed = !collapsed">
          <PanelLeftClose v-if="!collapsed" :size="17" />
          <PanelLeftOpen v-else :size="17" />
        </button>
      </div>

      <div v-if="!collapsed" class="workspace-label">WORKSPACE</div>
      <nav class="main-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to }"
          :title="collapsed ? item.label : undefined"
          @click="sidebarOpen = false"
        >
          <component :is="item.icon" :size="18" stroke-width="1.8" />
          <span v-if="!collapsed" class="nav-text">
            <span>{{ item.label }}</span>
            <small>{{ item.hint }}</small>
          </span>
          <span v-if="item.to === '/approvals' && !collapsed" class="nav-count">3</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <div class="system-status" :title="apiModeLabel()">
          <span class="status-dot" :class="{ live: apiConfig.enabled && apiConfig.usesTrustedHeaders }" />
          <span v-if="!collapsed">{{ apiModeLabel() }}</span>
        </div>
        <div v-if="!collapsed" class="sidebar-footnote">服务端事实优先 · v0.1</div>
      </div>
    </aside>

    <main class="main-column">
      <header class="topbar">
        <button class="icon-button mobile-menu" title="打开导航" @click="sidebarOpen = true">
          <Menu :size="20" />
        </button>
        <div class="breadcrumb"><span>Trade Agent</span><span class="breadcrumb-separator">/</span><strong>{{ pageTitle }}</strong></div>
        <div class="topbar-actions">
          <div class="connection-chip" :class="{ live: apiConfig.enabled && apiConfig.usesTrustedHeaders }">
            <Wifi :size="14" />
            <span>{{ apiModeLabel() }}</span>
          </div>
          <button class="icon-button notification" title="通知">
            <Bell :size="18" />
            <span class="notification-dot" />
          </button>
          <div class="user-menu">
            <span class="avatar">LW</span>
            <span class="user-meta"><strong>Liu Wei</strong><small>{{ roleLabel }}</small></span>
            <ChevronDown :size="15" class="muted-icon" />
          </div>
        </div>
      </header>

      <div class="page-content">
        <RouterView />
      </div>
    </main>
  </div>
</template>
