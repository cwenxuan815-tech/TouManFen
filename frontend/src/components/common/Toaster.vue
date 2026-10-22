<script setup lang="ts">
import { useToasts } from './toast'

const { toasts, dismissToast } = useToasts()
</script>

<template>
  <Teleport to="body">
    <!--
      F14：容器不再整体 aria-live（否则每条新增都会重复朗读整个容器）；
      每条 toast 自带 role/status/alert 与 aria-atomic，只播报新变化。
    -->
    <div class="toaster" aria-label="通知" role="region">
      <TransitionGroup name="toast">
        <article
          v-for="toast in toasts"
          :key="toast.id"
          class="toast"
          :class="`toast-${toast.kind}`"
          :role="toast.kind === 'error' ? 'alert' : 'status'"
          aria-atomic="true"
        >
          <span class="toast-icon" aria-hidden="true">{{
            toast.kind === 'error' || toast.kind === 'warning' ? '!' : 'i'
          }}</span>
          <div class="toast-copy">
            <b>{{ toast.message }}</b>
            <small v-if="toast.detail">{{ toast.detail }}</small>
            <small v-if="toast.requestId" class="toast-request">请求编号 {{ toast.requestId }}</small>
          </div>
          <button
            type="button"
            class="toast-close"
            aria-label="关闭通知"
            @click="dismissToast(toast.id)"
          >
            ×
          </button>
        </article>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toaster {
  position: fixed;
  top: 20px;
  right: 20px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: var(--space-8);
  width: min(360px, calc(100vw - 32px));
  pointer-events: none;
}
.toast {
  display: flex;
  align-items: flex-start;
  gap: var(--space-10);
  padding: var(--space-12) var(--space-14);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
  box-shadow: 0 14px 32px var(--toast-shadow);
  color: var(--text-primary);
  font-size: 11px;
  line-height: 1.6;
  pointer-events: auto;
}
.toast-error {
  border-color: var(--danger-border);
  background: var(--danger-soft);
  color: var(--danger-text);
}
.toast-warning {
  border-color: var(--warning-border);
  background: var(--warning-soft);
  color: var(--warning-text);
}
.toast-success {
  border-color: var(--success-soft);
  background: var(--success-soft);
  color: var(--success-text);
}
.toast-icon {
  flex: none;
  display: grid;
  place-items: center;
  width: 17px;
  height: 17px;
  border: 1px solid currentColor;
  border-radius: var(--radius-circle);
  font-size: 11px;
  font-weight: 700;
}
.toast-copy {
  min-width: 0;
  flex: 1;
}
.toast-copy b {
  display: block;
  font-size: 12px;
  font-weight: 700;
}
.toast-copy small {
  display: block;
  margin-top: var(--space-3);
  font-size: 10px;
  opacity: 0.9;
}
.toast-request {
  font-variant-numeric: tabular-nums;
}
.toast-close {
  flex: none;
  border: 0;
  background: transparent;
  color: currentColor;
  font-size: 16px;
  line-height: 1;
  opacity: 0.6;
}
.toast-close:hover {
  opacity: 1;
}
.toast-enter-active,
.toast-leave-active {
  transition: opacity var(--duration-fast), transform var(--duration-fast);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

/* F14：减少动效时 toast 直接出现/消失，不做位移动画。 */
@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active {
    transition: none;
  }
  .toast-enter-from,
  .toast-leave-to {
    transform: none;
  }
}
</style>
