<script setup lang="ts">
/**
 * F4：统一异步状态展示（idle/loading/empty/error/partial/success）。
 * 仅负责状态视觉与语义，不含业务请求；设计 token 来自 styles/tokens.css。
 */
import { computed } from 'vue'

export type AsyncStateKind =
  | 'idle'
  | 'loading'
  | 'empty'
  | 'error'
  | 'partial'
  | 'success'

const props = withDefaults(
  defineProps<{
    state: AsyncStateKind
    title?: string
    message?: string
    loadingText?: string
    retryLabel?: string
    requestId?: string
  }>(),
  {
    title: '',
    message: '',
    loadingText: '正在加载…',
    retryLabel: '',
    requestId: '',
  },
)

const emit = defineEmits<{ retry: [] }>()

const resolvedTitle = computed(() => {
  if (props.title) return props.title
  if (props.state === 'loading') return props.loadingText
  if (props.state === 'empty') return '暂无内容'
  if (props.state === 'error') return '暂时无法完成'
  if (props.state === 'partial') return '部分结果已返回'
  if (props.state === 'idle') return '等待开始'
  return ''
})

const resolvedMessage = computed(() => {
  if (props.message) return props.message
  if (props.state === 'partial') return '已展示可用结果，其余部分稍后重试。'
  if (props.state === 'empty') return '当前没有可展示的数据。'
  if (props.state === 'error') return '请稍后重试；若持续失败请检查服务状态。'
  return ''
})

const role = computed(() => (props.state === 'error' ? 'alert' : 'status'))
const live = computed(() => (props.state === 'error' ? 'assertive' : 'polite'))
</script>

<template>
  <div
    v-if="state === 'success'"
    class="async-state state-success"
    :role="role"
    :aria-live="live"
    aria-atomic="true"
  >
    <slot />
  </div>

  <div
    v-else-if="state === 'partial'"
    class="async-state state-partial"
    :role="role"
    :aria-live="live"
    aria-atomic="true"
  >
    <div class="state-banner">
      <span class="state-icon" aria-hidden="true">~</span>
      <div class="state-copy">
        <b>{{ resolvedTitle }}</b>
        <p v-if="resolvedMessage">{{ resolvedMessage }}</p>
      </div>
    </div>
    <slot />
  </div>

  <div
    v-else
    class="async-state"
    :class="`state-${state}`"
    :role="role"
    :aria-live="live"
    aria-atomic="true"
    :aria-busy="state === 'loading' ? 'true' : undefined"
  >
    <span
      class="state-icon"
      :class="{ 'state-spinner': state === 'loading' }"
      aria-hidden="true"
    >{{ state === 'loading' ? '' : state === 'error' ? '!' : state === 'empty' ? '-' : 'i' }}</span>
    <div class="state-copy">
      <b>{{ resolvedTitle }}</b>
      <p v-if="resolvedMessage">{{ resolvedMessage }}</p>
      <small v-if="requestId" class="state-request">请求编号 {{ requestId }}</small>
    </div>
    <button
      v-if="retryLabel && state === 'error'"
      type="button"
      class="state-retry"
      @click="emit('retry')"
    >
      {{ retryLabel }}
    </button>
    <div v-if="state === 'loading'" class="state-skeleton" aria-hidden="true">
      <i></i><i></i><i></i>
    </div>
  </div>
</template>

<style scoped>
.async-state {
  display: flex;
  align-items: flex-start;
  gap: var(--space-10);
  padding: var(--space-14) var(--space-17);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.6;
}
.state-success,
.state-partial {
  display: block;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: inherit;
}
.state-partial {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-10);
}
.state-banner {
  display: flex;
  align-items: flex-start;
  gap: var(--space-10);
  padding: var(--space-12) var(--space-14);
  border: 1px solid var(--warning-border);
  border-left: 3px solid var(--warning-strong);
  border-radius: var(--radius-md);
  background: var(--warning-soft);
  color: var(--warning-text);
}
.state-idle {
  background: var(--surface-page);
  border-style: dashed;
  color: var(--text-quiet);
}
.state-loading {
  background: var(--brand-tint);
  border-color: var(--brand-light);
  color: var(--text-primary);
}
.state-empty {
  background: var(--surface-page);
  border-style: dashed;
  color: var(--text-quiet);
}
.state-error {
  background: var(--danger-soft);
  border-color: var(--danger-border);
  color: var(--danger-text);
}
.state-icon {
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
.state-spinner {
  box-sizing: border-box;
  width: 13px;
  height: 13px;
  border: 2px solid var(--brand-light);
  border-top-color: var(--brand);
  animation: state-spin var(--duration-slow) linear infinite;
}
@keyframes state-spin {
  to {
    transform: rotate(360deg);
  }
}
.state-copy {
  min-width: 0;
}
.state-copy b {
  display: block;
  font-size: 12px;
}
.state-copy p {
  margin: var(--space-4) 0 0;
}
.state-request {
  display: block;
  margin-top: var(--space-5);
  font-size: 10px;
  opacity: 0.85;
}
.state-retry {
  margin-left: auto;
  border: 0;
  background: transparent;
  color: var(--brand);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.state-skeleton {
  width: 100%;
  max-width: 260px;
  margin-left: var(--space-10);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}
.state-skeleton i {
  display: block;
  height: 8px;
  border-radius: var(--radius-pill);
  background: var(--brand-light);
  opacity: 0.45;
}
.state-skeleton i:nth-child(2) {
  width: 82%;
}
.state-skeleton i:nth-child(3) {
  width: 64%;
}
@media (prefers-reduced-motion: reduce) {
  .state-spinner {
    animation: none;
  }
  .state-skeleton i {
    animation: none;
  }
}
</style>
