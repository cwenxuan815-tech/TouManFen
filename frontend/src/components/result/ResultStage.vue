<script setup lang="ts">
/**
 * F6：单模型结果；F9：对比模式的流式面板。
 *
 * F9 接入方式：
 * - 父组件（ClassifyPanel）每次提交流式对比时更新 `streamRequest.nonce`；
 * - 本组件持有唯一的 `useCompareStream` 实例，watch 到新请求才 start，
 *   切换模式 / 新提交 / 卸载时 stop 旧 EventSource（任意时刻只允许一条连接）；
 * - 旧 `POST /compare` 结果路径继续保留（`compare` prop，显式 submit 或回退展示）。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ClassifyResponse, CompareResponse, ModelInfo } from '../../types'
import { categoryInfo, modelLabel } from '../shared/meta'
import { useCompareStream, type CompareStreamRequest } from '../../composables'
import { pushToast } from '../common/toast'
import CompareBoard from './CompareBoard.vue'
import ResultCard from './ResultCard.vue'
import DemoBadge from '../placeholders/DemoBadge.vue'

const props = defineProps<{
  single: ClassifyResponse | null
  compare: CompareResponse | null
  title: string
  models: ModelInfo[]
  mode: 'single' | 'compare'
  /** F9：非空即开始/替换流式对比；null 表示停止。 */
  streamRequest: CompareStreamRequest | null
}>()

const stream = useCompareStream()
const streamSnapshot = computed(() => stream.snapshot.value)
const streamVisible = computed(
  () => props.mode === 'compare' && streamSnapshot.value.status !== 'idle',
)
const streamEyebrow = computed(() =>
  streamSnapshot.value.fallback
    ? 'MODEL COMPARISON · POST FALLBACK'
    : 'MODEL COMPARISON · STREAMING',
)
const streamHeading = computed(() => {
  const snapshot = streamSnapshot.value
  if (snapshot.status === 'error') return '对比没有完成，请稍后重试。'
  if (snapshot.status === 'done' || snapshot.status === 'fallback') {
    const completed = snapshot.summary?.ok ?? Object.keys(snapshot.arrived).length
    return snapshot.fallback
      ? `流式不可用，已回退完整请求：${completed} 个模型完成判断。`
      : `同一个标题，${completed} 个模型完成判断。`
  }
  return '正在逐个返回模型结果…'
})

/**
 * F14：结果播报只播报“变化”，且只播报一句摘要。
 *
 * 不再给整个结果区加 aria-live（否则一次更新会朗读整块卡片），
 * 改为一个视觉隐藏的 polite 状态区；同一 request_id 只播报一次。
 */
const liveMessage = ref('')
const announcedRequestId = ref('')

function announceResult(requestId: string, message: string): void {
  if (!requestId || requestId === announcedRequestId.value) return
  announcedRequestId.value = requestId
  liveMessage.value = message
}

watch(
  () => [
    props.single?.request_id ?? '',
    props.single?.pred_class ?? '',
    props.compare?.request_id ?? '',
    props.compare?.summary.ok ?? -1,
    streamSnapshot.value.status,
    streamSnapshot.value.requestId,
    streamSnapshot.value.summary?.ok ?? -1,
  ],
  () => {
    const single = props.single
    if (single) {
      announceResult(
        single.request_id,
        `分类完成：${categoryInfo(single.pred_class).zh}，由 ${modelLabel(props.models, single.model_id)} 判断。`,
      )
      return
    }
    const snapshot = streamSnapshot.value
    if (snapshot.status === 'error') return
    if (
      (snapshot.status === 'done' || snapshot.status === 'fallback') &&
      snapshot.requestId
    ) {
      const completed = snapshot.summary?.ok ?? Object.keys(snapshot.arrived).length
      announceResult(snapshot.requestId, `对比完成：${completed} 个模型完成判断。`)
      return
    }
    const compare = props.compare
    if (compare) {
      announceResult(
        compare.request_id,
        `对比完成：${compare.summary.ok} 个模型完成判断。`,
      )
    }
  },
  { immediate: true },
)

let lastNonce = 0

// F9：父组件用 nonce 表达「一次新提交」；同一请求重复渲染不会重连。
watch(
  () => props.streamRequest,
  (request) => {
    if (!request) {
      stream.stop()
      return
    }
    if (request.nonce === lastNonce) return
    lastNonce = request.nonce
    stream.start(request.title, request.models)
  },
  { immediate: true },
)

// 切换回单模型模式即关闭对比连接（不产生用户可见错误）。
watch(
  () => props.mode,
  (nextMode, previousMode) => {
    if (previousMode === 'compare' && nextMode !== 'compare') stream.stop()
  },
)

// F3/F4：回退 POST 失败走 Toaster；SSE 流级错误在面板内由 AsyncState 呈现，避免重复横幅。
watch(
  () => stream.error.value,
  (message) => {
    if (!message || stream.status.value !== 'error') return
    if (stream.streamError.value) return
    pushToast({
      kind: 'error',
      message,
      requestId: stream.requestId.value || undefined,
    })
  },
)

onBeforeUnmount(() => {
  stream.stop()
})

/**
 * 仅用于 F9 测试 harness / 调试：读取流式状态（含首事件耗时、回退标记）。
 * 不改变渲染行为，也不对外暴露连接对象。
 */
defineExpose({ stream })
</script>

<template>
  <p class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-live="result">
    {{ liveMessage }}
  </p>

  <Transition name="result-reveal">
    <section v-if="single" class="result-section">
      <div class="result-heading"><div><div class="eyebrow"><span class="eyebrow-line"></span> CLASSIFICATION RESULT</div><h3>这条新闻，属于 <span>{{ categoryInfo(single.pred_class).zh }}</span></h3></div><div class="result-heading-side"><DemoBadge v-if="single.data_source" :source="single.data_source" /><span class="result-request">REQUEST · {{ single.request_id.slice(0, 8) }}</span></div></div>
      <ResultCard :single="single" :title="title" :models="models" />
    </section>

    <section
      v-else-if="streamVisible"
      class="result-section"
      data-result-mode="stream"
    >
      <div class="result-heading">
        <div>
          <div class="eyebrow"><span class="eyebrow-line"></span> {{ streamEyebrow }}</div>
          <h3>{{ streamHeading }}</h3>
        </div>
        <span v-if="streamSnapshot.requestId" class="result-request">
          REQUEST · {{ streamSnapshot.requestId.slice(0, 8) }}
        </span>
      </div>
      <CompareBoard :stream="streamSnapshot" :models="models" :title="title" />
    </section>

    <section v-else-if="compare" class="result-section" data-result-mode="post">
      <div class="result-heading"><div><div class="eyebrow"><span class="eyebrow-line"></span> MODEL COMPARISON</div><h3>同一个标题，<span>{{ compare.summary.ok }} 个模型完成判断。</span></h3></div><span class="result-request">REQUEST · {{ compare.request_id.slice(0, 8) }}</span></div>
      <CompareBoard :data="compare" :models="models" :title="title" />
    </section>
  </Transition>
</template>

<style scoped>
  .result-section {
    margin-top: 45px;
  }

  .result-heading {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    margin-bottom: 18px;
  }

  .result-heading-side {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: var(--space-8);
  }

  .result-heading h3 {
    margin: 13px 0 0;
    font-size: 26px;
    letter-spacing: -.04em;
  }

  .result-heading h3 span {
    color: var(--brand);
  }

  .result-request {
    color: #9ba4b2;
    font-size: 9px;
    letter-spacing: .1em;
  }

  .single-result-card {
    min-height: 220px;
    padding: 34px 42px;
    position: relative;
    display: flex;
    align-items: center;
    gap: 28px;
    border-radius: var(--radius-4xl);
    overflow: hidden;
    background: var(--cat-blue-bg);
    color: #193460;
  }

  .result-category-mark {
    width: 82px;
    height: 82px;
    flex: none;
    display: grid;
    place-items: center;
    border-radius: 26px;
    background: #ffffffa8;
    box-shadow: 0 15px 36px #4977bf20;
    font-size: 43px;
    font-weight: 800;
    color: var(--cat-blue-mark);
  }

  .result-main {
    position: relative;
    z-index: 1;
  }

  .result-kicker {
    font-size: 10px;
    letter-spacing: .18em;
    text-transform: uppercase;
    color: #6280b3;
    font-weight: 700;
  }

  .result-category-name {
    font-size: 45px;
    line-height: 1.25;
    font-weight: 700;
    letter-spacing: -.05em;
    margin-top: 3px;
  }

  .result-context {
    margin-top: var(--space-10);
    color: #6f819e;
    font-size: 12px;
  }

  .result-meta {
    position: relative;
    z-index: 1;
    margin-left: auto;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--space-5);
    color: #6c7f9d;
    font-size: 10px;
  }

  .result-meta b {
    font-size: 32px;
    line-height: 1.1;
    color: #2d5eaf;
    font-weight: 600;
  }

  .result-meta b small {
    font-size: 12px;
    font-weight: 500;
  }

  .result-orbit {
    position: absolute;
    border: 1px solid var(--border-on-art);
    border-radius: var(--radius-circle);
    pointer-events: none;
  }

  .result-orbit-a {
    width: 330px;
    height: 190px;
    right: 50px;
    top: 12px;
    transform: rotate(-28deg);
  }

  .result-orbit-b {
    width: 410px;
    height: 105px;
    right: -14px;
    top: 47px;
    transform: rotate(18deg);
    border-color: #b3d0ff;
  }

  .tone-violet {
    background: var(--cat-violet-bg);
  }

  .tone-cyan {
    background: var(--cat-cyan-bg);
  }

  .tone-amber {
    background: var(--cat-amber-bg);
  }

  .tone-indigo {
    background: var(--cat-indigo-bg);
  }

  .tone-green {
    background: var(--cat-green-bg);
  }

  .tone-red {
    background: var(--cat-red-bg);
  }

  .tone-orange {
    background: var(--cat-orange-bg);
  }

  .tone-pink {
    background: var(--cat-pink-bg);
  }

  .tone-purple {
    background: var(--cat-purple-bg);
  }

  .tone-violet .result-category-mark {
    color: var(--cat-violet-mark);
  }

  .tone-cyan .result-category-mark {
    color: var(--cat-cyan-mark);
  }

  .tone-amber .result-category-mark {
    color: var(--cat-amber-mark);
  }

  .tone-indigo .result-category-mark {
    color: var(--cat-indigo-mark);
  }

  .tone-green .result-category-mark {
    color: var(--cat-green-mark);
  }

  .tone-red .result-category-mark {
    color: var(--cat-red-mark);
  }

  .tone-orange .result-category-mark {
    color: var(--cat-orange-mark);
  }

  .tone-pink .result-category-mark {
    color: var(--cat-pink-mark);
  }

  .tone-purple .result-category-mark {
    color: var(--cat-purple-mark);
  }

  .result-reveal-enter-active {
    transition: opacity var(--duration-medium) ease,transform var(--duration-medium) ease;
  }

  .result-reveal-enter-from {
    opacity: 0;
    transform: translateY(12px);
  }

  @media (prefers-reduced-motion: reduce) {
    .result-reveal-enter-active {
      transition: none;
    }
  }

  @media(max-width:760px) {
    .result-section {
      margin-top: 34px;
    }
    .result-heading {
      align-items: flex-start;
      gap: var(--space-10);
    }
    .result-heading h3 {
      font-size: 19px;
    }
    .result-request {
      font-size: 8px;
      white-space: nowrap;
      margin-top: var(--space-14);
    }
    .single-result-card {
      min-height: 190px;
      padding: var(--space-24) 20px;
      gap: var(--space-16);
      flex-wrap: wrap;
    }
    .result-category-mark {
      width: 61px;
      height: 61px;
      border-radius: 20px;
      font-size: 31px;
    }
    .result-category-name {
      font-size: 35px;
    }
    .result-context {
      font-size: 10px;
      max-width: 58vw;
    }
    .result-meta {
      width: 100%;
      margin-left: 77px;
      align-items: flex-start;
      display: grid;
      grid-template-columns: auto auto;
      column-gap: 9px;
    }
    .result-meta span:first-child {
      grid-column: 1/-1;
    }
    .result-meta b {
      font-size: 24px;
    }
    .result-meta span:last-child {
      align-self: center;
    }
  }

  @media(max-width:390px) {
    .result-request {
      display: none;
    }
  }
</style>
