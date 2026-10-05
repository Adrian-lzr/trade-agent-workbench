<script setup lang="ts">
import { CircleAlert, Info, ShieldAlert } from "lucide-vue-next";

const props = withDefaults(defineProps<{ tone?: "info" | "warning" | "danger"; title: string; detail: string }>(), {
  tone: "info",
});
</script>

<template>
  <div class="callout" :class="`callout-${props.tone}`">
    <ShieldAlert v-if="props.tone === 'danger'" :size="18" />
    <CircleAlert v-else-if="props.tone === 'warning'" :size="18" />
    <Info v-else :size="18" />
    <div><strong>{{ title }}</strong><p>{{ detail }}</p></div>
  </div>
</template>
