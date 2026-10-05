import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import App from "./App.vue";
import InboxView from "./views/InboxView.vue";
import ReviewView from "./views/ReviewView.vue";
import ApprovalView from "./views/ApprovalView.vue";
import ExportView from "./views/ExportView.vue";
import AnalyticsView from "./views/AnalyticsView.vue";
import "./styles.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/inbox" },
    { path: "/inbox", component: InboxView, meta: { title: "询盘工作台" } },
    { path: "/review", component: ReviewView, meta: { title: "版本审核" } },
    { path: "/approvals", component: ApprovalView, meta: { title: "审批队列" } },
    { path: "/exports", component: ExportView, meta: { title: "文件导出" } },
    { path: "/analytics", component: AnalyticsView, meta: { title: "经营指标" } },
  ],
});

createApp(App).use(router).mount("#app");
