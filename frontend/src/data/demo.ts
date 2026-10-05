import type { Inquiry, MetricCard, QuoteRevision } from "../types";

export const inquiries: Inquiry[] = [
  {
    id: "RFQ-2026-1048",
    customer: "Northwind Industrial",
    country: "United States",
    subject: "M8 stainless bolts for Q4 line",
    received: "10 min ago",
    owner: "Liu Wei",
    status: "needs_review",
    quote: "QT-1048",
    revision: "v3",
    runId: "run-demo-1048",
  },
  {
    id: "RFQ-2026-1045",
    customer: "Hanseatic Components",
    country: "Germany",
    subject: "A2-70 fastener assortment",
    received: "1 hr ago",
    owner: "Liu Wei",
    status: "waiting_input",
    quote: "QT-1045",
    revision: "v2",
    runId: "run-demo-1045",
  },
  {
    id: "RFQ-2026-1041",
    customer: "Sakura Motion",
    country: "Japan",
    subject: "Production run replenishment",
    received: "Yesterday",
    owner: "Chen Jia",
    status: "in_progress",
    quote: "QT-1041",
    revision: "v1",
    runId: "run-demo-1041",
  },
  {
    id: "RFQ-2026-1036",
    customer: "Atlas Fabrication",
    country: "Canada",
    subject: "M10 hex cap screws",
    received: "Yesterday",
    owner: "Liu Wei",
    status: "ready",
    quote: "QT-1036",
    revision: "v4",
    runId: "run-demo-1036",
  },
];

export const quoteRevisions: QuoteRevision[] = [
  {
    id: "qrev-1048-v3",
    quoteId: "QT-1048",
    version: "v3",
    createdAt: "2026-10-01 14:24",
    createdBy: "Liu Wei",
    status: "in_review",
    contentHash: "sha256:7df1…4c92",
    amount: "400.00",
    currency: "USD",
    validity: "Valid through 2026-10-31",
    items: [
      {
        sku: "BOLT-M8-30-A2",
        description: "Stainless steel hex bolt, M8 × 30, A2-70",
        quantity: "5,000",
        unit: "piece",
        unitPrice: "0.08",
        total: "400.00",
      },
    ],
    blockers: [
      {
        code: "DELIVERY_UNCONFIRMED",
        title: "Delivery lead time is not confirmed",
        detail: "The RFQ asks for Q4 delivery, but the customer did not provide a target date and no approved lead-time evidence is attached.",
        severity: "blocking",
      },
      {
        code: "CERTIFICATE_EVIDENCE_MISSING",
        title: "Material certificate evidence is missing",
        detail: "Do not add an A2 certificate promise until the applicable document is attached and reviewed.",
        severity: "warning",
      },
    ],
  },
  {
    id: "qrev-1048-v2",
    quoteId: "QT-1048",
    version: "v2",
    createdAt: "2026-10-01 11:52",
    createdBy: "Liu Wei",
    status: "superseded",
    contentHash: "sha256:2b1c…909a",
    amount: "400.00",
    currency: "USD",
    validity: "Valid through 2026-10-31",
    items: [
      {
        sku: "BOLT-M8-30-A2",
        description: "Stainless steel hex bolt, M8 × 30, A2-70",
        quantity: "5,000",
        unit: "piece",
        unitPrice: "0.08",
        total: "400.00",
      },
    ],
    blockers: [],
  },
];

export const metrics: MetricCard[] = [
  { label: "待审核报价", value: "12", change: "+3", trend: "up", note: "相比上周" },
  { label: "首份草稿中位时长", value: "18m", change: "−4m", trend: "down", note: "过去 30 天" },
  { label: "报价阻断率", value: "22%", change: "−6pp", trend: "down", note: "过去 30 天" },
  { label: "已批准金额", value: "$18.4k", change: "+12%", trend: "up", note: "当前有效版本" },
];

export const eventLabels: Record<string, string> = {
  "run.created": "运行已创建",
  "run.resumed": "运行已恢复",
  "run.waiting_input": "等待人工输入",
  "workflow.extraction_completed": "询盘字段已提取",
  "workflow.quote_ready": "报价草稿已生成",
};
