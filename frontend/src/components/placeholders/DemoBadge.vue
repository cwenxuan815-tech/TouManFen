<script setup lang="ts">
/**
 * F15：演示真实性徽标。
 *
 * 只渲染调用方显式给出的 source；未给出 source 时完全不渲染。
 * `unknown` 只能由显式状态传入，不能由“后端可达/worker up”推导。
 */
import { DEMO_LABELS, type DemoSource } from '../../composables/useDemoState'

defineProps<{
  source?: DemoSource
  label?: string
  detail?: string
  hint?: string
}>()
</script>

<template>
  <span
    v-if="source"
    class="demo-badge"
    :class="`demo-${source}`"
    role="status"
    aria-live="polite"
  >
    <i aria-hidden="true"></i>
    <span class="demo-label">{{ label || DEMO_LABELS[source] }}</span>
    <small v-if="detail" class="demo-detail">{{ detail }}</small>
  </span>
  <span v-if="hint" class="demo-hint" role="note">{{ hint }}</span>
</template>

<style scoped>
.demo-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-5) var(--space-9);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 10px;
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
}
.demo-badge i {
  width: 6px;
  height: 6px;
  border-radius: var(--radius-circle);
  background: currentColor;
}
.demo-live {
  border-color: var(--success-soft);
  background: var(--success-soft);
  color: var(--success-text);
}
.demo-mock {
  border-color: var(--warning-border);
  background: var(--warning-soft);
  color: var(--warning-text);
}
.demo-cached,
.demo-unknown {
  border-color: var(--border-card);
  background: var(--surface-page);
  color: var(--text-quiet);
}
.demo-detail {
  font-size: 9px;
  font-weight: 400;
  opacity: 0.85;
}
.demo-hint {
  display: inline-block;
  margin-left: var(--space-8);
  color: var(--text-quiet);
  font-size: 10px;
}
</style>
