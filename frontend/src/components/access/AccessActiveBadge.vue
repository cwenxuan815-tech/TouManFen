<script setup lang="ts">
/** 阶段 5C：active 期间的全局倒计时与提前离开入口。 */
import { computed } from 'vue'

const props = defineProps<{
  remainingSeconds: number | null
  busy: boolean
}>()

const emit = defineEmits<{
  leave: []
}>()

const countdown = computed(() => {
  const total = Math.max(0, Math.ceil(props.remainingSeconds ?? 0))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
})
</script>

<template>
  <aside class="access-active" role="status" aria-live="polite" data-access-active>
    <span class="access-active-dot" aria-hidden="true"></span>
    <span class="access-active-label">访问名额</span>
    <strong class="access-active-time" data-access-active-countdown>{{ countdown }}</strong>
    <button
      type="button"
      class="access-active-leave"
      data-access-leave
      :disabled="busy"
      @click="emit('leave')"
    >
      提前离开
    </button>
  </aside>
</template>

<style scoped>
.access-active {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  width: min(100% - 24px, var(--layout-wide, 1180px));
  margin: 10px auto 0;
  padding: 8px 12px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 11px;
}
.access-active-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--brand-dark);
}
.access-active-label {
  font-weight: 700;
  color: var(--text-primary);
}
.access-active-time {
  color: var(--brand-dark);
  font-variant-numeric: tabular-nums;
}
.access-active-leave {
  margin-left: auto;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  cursor: pointer;
}
.access-active-leave:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
@media (max-width: 390px) {
  .access-active {
    width: auto;
    margin: 8px 12px 0;
    font-size: 10px;
  }
  .access-active-leave {
    margin-left: 0;
  }
}
</style>
