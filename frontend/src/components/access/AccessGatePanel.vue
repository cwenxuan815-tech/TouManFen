<script setup lang="ts">
/**
 * 阶段 5C：全局访问状态面板。
 * 只显示服务端状态；不显示 IP/ticket；不自行判断租约是否续期。
 */
import { computed } from 'vue'
import type { AccessMode } from '../../composables/useAccess'

const props = defineProps<{
  mode: AccessMode
  position: number | null
  ahead: number | null
  remainingSeconds: number | null
  errorMessage: string
  busy: boolean
}>()

const emit = defineEmits<{
  cancel: []
  retry: []
}>()

const title = computed(() => {
  switch (props.mode) {
    case 'checking':
      return '正在确认访问名额…'
    case 'queued':
      return '正在排队'
    case 'ready':
      return '轮到你了'
    case 'cooling':
      return '访问冷却中'
    case 'queue_full':
      return '排队人数已满'
    case 'cancelled':
      return '本次访问已结束'
    case 'unavailable':
      return '访问管理暂不可用'
    case 'identity':
      return '无法确认访问身份'
    default:
      return '访问暂不可用'
  }
})

const description = computed(() => {
  switch (props.mode) {
    case 'checking':
      return '请稍候，正在恢复访问状态。'
    case 'queued':
      return props.position
        ? `当前第 ${props.position} 位，前方还有 ${Math.max(0, props.ahead ?? props.position - 1)} 人。`
        : '正在等待前方访问结束。'
    case 'ready':
      return '名额已保留，正在自动进入，请稍候。'
    case 'cooling':
      return '本次使用已满 3 分钟，冷却结束后可重新进入。'
    case 'queue_full':
      return '当前排队人数已达上限，请稍后手动重试。'
    case 'cancelled':
      return '名额已释放。需要时请重新进入。'
    case 'unavailable':
      return '网关访问管理暂时不可用，业务 API 已停止放行。'
    case 'identity':
      return '无法依据可信网络身份进入，业务 API 已停止放行。'
    default:
      return props.errorMessage || '访问状态异常，请稍后重试。'
  }
})

const countdownText = computed(() => {
  if (props.remainingSeconds === null) return ''
  const seconds = Math.max(0, Math.ceil(props.remainingSeconds))
  if (props.mode === 'active') return `剩余 ${seconds} 秒`
  if (props.mode === 'cooling') return `${seconds} 秒后可重试`
  if (props.mode === 'ready') return `${seconds} 秒内完成进入`
  if (props.mode === 'queued') return `排队资格剩余 ${seconds} 秒`
  return ''
})

const canCancel = computed(() => props.mode === 'queued' && !props.busy)
const canRetry = computed(
  () =>
    !props.busy
    && ['queue_full', 'cancelled', 'unavailable', 'identity', 'error'].includes(
      props.mode,
    ),
)
</script>

<template>
  <section
    class="access-overlay"
    role="status"
    aria-live="polite"
    data-access-panel
    :data-access-state="mode"
  >
    <div class="access-card">
      <p class="access-kicker">TMF · SERVICE ACCESS</p>
      <h2 class="access-title">{{ title }}</h2>
      <p class="access-description" data-access-description>{{ description }}</p>
      <p
        v-if="countdownText"
        class="access-countdown"
        data-access-countdown
      >
        {{ countdownText }}
      </p>
      <p v-if="mode === 'queued' && position !== null" class="access-position" data-access-position>
        第 {{ position }} 位
        <span v-if="ahead !== null">· 前方 {{ Math.max(0, ahead) }} 人</span>
      </p>
      <p v-if="mode === 'queue_full'" class="access-note" data-access-queue-full>
        不会自动高频重试，避免加重排队。
      </p>
      <p v-if="errorMessage && mode === 'error'" class="access-error" data-access-error>
        {{ errorMessage }}
      </p>
      <div class="access-actions">
        <button
          v-if="canCancel"
          type="button"
          class="access-button access-button-secondary"
          data-access-cancel
          @click="emit('cancel')"
        >
          取消排队
        </button>
        <button
          v-if="canRetry"
          type="button"
          class="access-button"
          data-access-retry
          @click="emit('retry')"
        >
          {{ mode === 'queue_full' ? '重试进入' : '重新进入' }}
        </button>
      </div>
      <p class="access-foot">访问名额按客户端 IP 分配；不是账号认证。</p>
    </div>
  </section>
</template>

<style scoped>
.access-overlay {
  position: fixed;
  inset: 0;
  z-index: 160;
  display: grid;
  place-items: center;
  padding: 20px;
  background: #0c1728b8;
  backdrop-filter: blur(8px);
  overflow-y: auto;
}
.access-card {
  width: min(92vw, 480px);
  box-sizing: border-box;
  padding: 28px 24px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-3xl);
  background: var(--surface-card);
  color: var(--text-primary);
  box-shadow: 0 28px 70px #0715273d;
  overflow-wrap: anywhere;
}
.access-kicker {
  margin: 0 0 8px;
  color: var(--text-quiet);
  font-size: 10px;
  letter-spacing: 0.12em;
}
.access-title {
  margin: 0;
  font-size: clamp(22px, 5vw, 30px);
  line-height: 1.25;
}
.access-description {
  margin: 12px 0 0;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.7;
}
.access-countdown,
.access-position {
  margin: 16px 0 0;
  color: var(--brand-dark);
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.access-position {
  font-size: 14px;
}
.access-note,
.access-error,
.access-foot {
  margin: 12px 0 0;
  color: var(--text-quiet);
  font-size: 11px;
  line-height: 1.6;
}
.access-error {
  color: var(--danger-text, #a33232);
}
.access-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 20px;
}
.access-button {
  min-height: 44px;
  padding: 0 18px;
  border: 1px solid var(--brand-dark);
  border-radius: var(--radius-pill);
  background: var(--brand-dark);
  color: #fff;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.access-button-secondary {
  background: transparent;
  color: var(--brand-dark);
}
.access-button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
@media (max-width: 390px) {
  .access-overlay {
    padding: 12px;
  }
  .access-card {
    width: 100%;
    padding: 22px 16px;
    border-radius: var(--radius-2xl);
  }
  .access-actions {
    flex-direction: column;
  }
  .access-button {
    width: 100%;
  }
}
</style>
