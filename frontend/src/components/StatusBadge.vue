<script setup lang="ts">
import { computed } from "vue";
import { CircleAlert, CircleCheck, Clock3, LoaderCircle } from "lucide-vue-next";

const props = withDefaults(
  defineProps<{ status: string; label?: string }>(),
  { label: undefined },
);

const metadata: Record<string, { label: string; tone: string; icon: typeof Clock3 }> = {
  needs_review: { label: "待审核", tone: "amber", icon: CircleAlert },
  waiting_input: { label: "待补充", tone: "blue", icon: Clock3 },
  in_progress: { label: "处理中", tone: "slate", icon: LoaderCircle },
  ready: { label: "可提交", tone: "green", icon: CircleCheck },
  draft: { label: "草稿", tone: "slate", icon: Clock3 },
  in_review: { label: "审核中", tone: "amber", icon: CircleAlert },
  approved: { label: "已批准", tone: "green", icon: CircleCheck },
  rejected: { label: "已驳回", tone: "red", icon: CircleAlert },
  superseded: { label: "已被替代", tone: "slate", icon: Clock3 },
  waiting: { label: "等待中", tone: "blue", icon: Clock3 },
};

const value = computed(() => metadata[props.status] ?? { label: props.status, tone: "slate", icon: Clock3 });
const text = computed(() => props.label ?? value.value.label);
</script>

<template>
  <span class="status-badge" :class="`tone-${value.tone}`">
    <component :is="value.icon" :size="13" :class="{ spin: props.status === 'in_progress' }" />
    {{ text }}
  </span>
</template>
