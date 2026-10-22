<script setup lang="ts">
import { computed, toRef, watch } from 'vue'
import { exampleTitles, categoryInfo, MAX_TITLE_CHARS, modelLabel } from '../shared/meta'
import type { ModelInfo } from '../../types'
import { useClassify } from '../../composables'
import InputBox from '../classify/InputBox.vue'
import ResultStage from '../result/ResultStage.vue'
import AsyncState from '../common/AsyncState.vue'
import { pushToast } from '../common/toast'

const props = defineProps<{
  models: ModelInfo[]
  defaultModel: string
  loadingModels: boolean
  modelsError: string
}>()

const emit = defineEmits<{
  'retry-models': []
}>()

const {
  title,
  mode,
  selectedModel,
  selectedModels,
  includeLlm,
  submitting,
  pageError,
  lastTitle,
  lastRequestId,
  realtimeResult,
  realtimeStatus,
  onCompositionStart,
  onCompositionEnd,
  selectedModelNames,
  canSubmit,
  singleResult,
  compareResult,
  pageHeading,
  compareStreamRequest,
  setMode,
  onTitleUpdate,
  chooseExample,
  toggleModel,
  activateLlm,
  submit,
  submitCompareStream,
} = useClassify({
  models: toRef(props, 'models'),
  defaultModel: toRef(props, 'defaultModel'),
  modelsError: toRef(props, 'modelsError'),
})

const defaultModelLabel = computed(() => modelLabel(props.models, props.defaultModel))
const selectedModelLabel = computed(() => modelLabel(props.models, selectedModel.value))

/**
 * F9：对比模式优先走 SSE（ResultStage 内负责连接生命周期）；
 * 当前环境不支持 EventSource 时回退到旧 `submit()` POST 路径。
 */
function onSubmit(): void {
  if (mode.value === 'compare' && submitCompareStream()) return
  void submit()
}

// F3：页面错误状态保持完整；F4 统一以 Toaster 展示，避免与内联横幅重复。
watch(pageError, (message) => {
  if (!message) return
  pushToast({
    kind: 'error',
    message,
    requestId: lastRequestId.value || undefined,
  })
})
</script>

<template>
  <section id="lab" class="lab-section" aria-labelledby="lab-title">
    <div class="section-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> CLASSIFICATION STUDIO</div>
        <h2 id="lab-title">从一个标题开始。</h2>
      </div>
      <p>选择模型，输入新闻标题，<br class="desktop-break" />观察分类结果与模型差异。</p>
    </div>

    <div class="studio-card">
      <div class="studio-topline">
        <div class="studio-label"><span class="studio-icon">✳</span> 分类工作台 <span class="label-en">/ WORKSPACE</span></div>
        <div class="studio-live"><span class="live-dot"></span> {{ loadingModels ? '正在读取模型状态' : modelsError ? '等待后端连接' : `${models.filter((item) => item.enabled).length} 个模型已登记` }}</div>
      </div>

      <div class="mode-switch" role="tablist" aria-label="分类模式">
        <button type="button" role="tab" :aria-selected="mode === 'single'" :class="{ active: mode === 'single' }" @click="setMode('single')">
          <span class="mode-icon">◉</span> 单模型分类 <small>选择一个模型</small>
        </button>
        <button type="button" role="tab" :aria-selected="mode === 'compare'" :class="{ active: mode === 'compare' }" @click="setMode('compare')">
          <span class="mode-icon compare-icon">↗</span> 模型对比 <small>并行查看结果</small>
        </button>
      </div>

      <AsyncState
        v-if="modelsError"
        class="panel-status"
        state="error"
        :message="modelsError"
        retry-label="重新连接"
        @retry="emit('retry-models')"
      />
      <div v-if="includeLlm" class="inline-alert alert-warn panel-status" role="status">
        <span class="alert-symbol">i</span><span>已启用大模型。请确认后端 8105 当前连接的是本地 mock；若是真实供应商，调用可能产生费用。</span>
      </div>

      <div class="workspace-grid">
        <div class="input-column">
          <InputBox
            :model-value="title"
            :examples="exampleTitles"
            :max-chars="MAX_TITLE_CHARS"
            @update:model-value="onTitleUpdate"
            @select-example="chooseExample"
            @composition-start="onCompositionStart"
            @composition-end="onCompositionEnd"
          />

          <div v-if="realtimeStatus === 'loading' || realtimeResult" class="realtime-strip" role="status" aria-live="polite">
            <span v-if="realtimeStatus === 'loading'" class="realtime-dot"></span>
            <span v-if="realtimeStatus === 'loading'">实时分析中…</span>
            <template v-else-if="realtimeResult">
              <span class="realtime-label">实时建议（fastText）</span>
              <b>{{ categoryInfo(realtimeResult.pred_class).zh }}</b>
              <small>仅作输入提示，未校准</small>
            </template>
          </div>

          <div v-if="mode === 'single'" class="model-select-wrap">
            <label class="field-label" for="model-select"><span>选择分类模型</span><span class="field-hint">默认：{{ defaultModelLabel }}</span></label>
            <div class="select-shell">
              <select id="model-select" v-model="selectedModel" :disabled="loadingModels || !!modelsError">
                <option v-for="model in models.filter((item) => item.enabled)" :key="model.model_id" :value="model.model_id" :disabled="model.model_id === 'llm' && !includeLlm">
                  {{ model.display_name }}{{ model.model_id === 'llm' && !includeLlm ? ' · 需手动启用' : '' }}
                </option>
              </select>
              <span class="select-chevron">⌄</span>
            </div>
          </div>

          <div v-else id="model-selection" class="models-select-wrap">
            <div class="field-label"><span>参与对比的模型</span><span class="field-hint">{{ selectedModels.length }} 个已选择</span></div>
            <div v-if="loadingModels" class="model-picker-loading">正在读取模型列表…</div>
            <div v-else class="model-picker">
              <button
                v-for="model in models.filter((item) => item.enabled)"
                :key="model.model_id"
                type="button"
                class="model-pill"
                :class="{ chosen: selectedModels.includes(model.model_id), 'model-pill-locked': model.model_id === 'llm' && !includeLlm }"
                :aria-pressed="selectedModels.includes(model.model_id)"
                :disabled="model.model_id === 'llm' && !includeLlm"
                @click="toggleModel(model.model_id)"
              >
                <span class="pill-check">{{ selectedModels.includes(model.model_id) ? '✓' : '' }}</span>
                {{ model.display_name }}
                <span v-if="model.model_id === 'llm' && !includeLlm" class="pill-lock">需启用</span>
              </button>
            </div>
          </div>

          <label class="llm-consent" :class="{ 'llm-consent-on': includeLlm }">
            <input v-model="includeLlm" type="checkbox" @change="activateLlm" />
            <span class="custom-check">{{ includeLlm ? '✓' : '' }}</span>
            <span class="consent-copy"><b>本次允许调用大模型</b><small>真实供应商尚未验证；调用可能产生费用。默认关闭。</small></span>
            <span class="consent-tag">LLM</span>
          </label>
        </div>

        <aside class="action-column">
          <div class="action-card">
            <div class="action-card-kicker">{{ mode === 'single' ? 'ONE MODEL' : 'SIDE BY SIDE' }}</div>
            <h3>{{ pageHeading }}</h3>
            <p>{{ mode === 'single' ? '用一个模型，快速找到标题所属的新闻频道。' : '让多个模型并行判断，比较它们对标题的理解。' }}</p>

            <div class="action-summary">
              <span class="summary-icon">✦</span>
              <span><b>{{ mode === 'single' ? selectedModelLabel || '选择一个模型' : `${selectedModels.length} 个模型` }}</b><small>{{ mode === 'single' ? '单模型推理' : selectedModelNames.join(' · ') || '至少选择一个模型' }}</small></span>
            </div>

            <button class="submit-button" type="button" :disabled="!canSubmit" @click="onSubmit">
              <span v-if="submitting" class="button-spinner"></span>
              <span>{{ submitting ? '正在分析标题' : mode === 'single' ? '开始分类' : '开始模型对比' }}</span>
              <span v-if="!submitting" class="button-arrow">↗</span>
            </button>
            <div class="privacy-note"><span class="lock-icon">⌑</span> 标题仅用于本次推理，不会显示在服务日志中</div>
          </div>

          <div class="mini-stat-row">
            <div><b>10</b><span>新闻频道</span></div>
            <i></i>
            <div><b>{{ models.filter((item) => item.enabled).length || '—' }}</b><span>可用模型</span></div>
            <i></i>
            <div><b>API</b><span>本地推理</span></div>
          </div>
        </aside>
      </div>
    </div>

    <ResultStage
      :single="singleResult"
      :compare="compareResult"
      :title="lastTitle"
      :models="models"
      :mode="mode"
      :stream-request="compareStreamRequest"
    />
  </section>
</template>

<style scoped>
  .panel-status {
    margin-bottom: var(--space-14);
  }

  .realtime-strip {
    display: flex;
    align-items: center;
    gap: var(--space-8);
    margin-top: var(--space-10);
    padding: var(--space-8) var(--space-12);
    border: 1px dashed var(--border-control);
    border-radius: var(--radius-md);
    background: var(--surface-page);
    color: var(--text-quiet);
    font-size: 10px;
    min-height: 30px;
  }

  .realtime-label {
    color: var(--text-faint);
  }

  .realtime-strip b {
    color: var(--brand);
    font-size: 11px;
  }

  .realtime-strip small {
    color: var(--text-subtle);
  }

  .realtime-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-circle);
    background: var(--brand);
    animation: realtime-pulse var(--duration-pulse) infinite;
  }

  @keyframes realtime-pulse {
    50% {
      opacity: 0.35;
    }
  }

  /* F14：减少动效时停止脉冲/旋转，保留静态状态。 */
  @media (prefers-reduced-motion: reduce) {
    .realtime-dot,
    .button-spinner {
      animation: none;
    }
    .submit-button,
    .model-pill,
    .mode-switch button,
    .llm-consent,
    .select-shell select {
      transition: none;
    }
  }

  .live-dot {
    width: 7px;
    height: 7px;
    border-radius: var(--radius-circle);
    background: #b2bac7;
  }

  .studio-card {
    background: var(--surface-card);
    border: 1px solid var(--border-card);
    border-radius: var(--radius-5xl);
    padding: 25px var(--space-30) 26px;
    box-shadow: 0 24px 80px #2a3f6010;
  }

  .studio-topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 21px;
    border-bottom: 1px solid #edf0f4;
  }

  .studio-label {
    font-size: 15px;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: var(--space-9);
  }

  .studio-icon {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: var(--radius-xs);
    background: #edf4ff;
    color: var(--brand-strong);
    font-size: 17px;
  }

  .label-en {
    font-size: 10px;
    font-weight: 500;
    letter-spacing: .13em;
    color: #9aa2af;
  }

  .studio-live {
    display: flex;
    align-items: center;
    gap: var(--space-8);
    color: #8c95a2;
    font-size: 11px;
  }

  .live-dot {
    background: #39b780;
    box-shadow: var(--ring-success);
  }

  .mode-switch {
    display: flex;
    gap: var(--space-8);
    padding: 18px 0 22px;
  }

  .mode-switch button {
    position: relative;
    min-width: 194px;
    padding: var(--space-12) var(--space-15);
    display: flex;
    align-items: center;
    gap: var(--space-8);
    border: 1px solid transparent;
    border-radius: var(--radius-md);
    background: transparent;
    text-align: left;
    color: #798290;
    font-size: 12px;
    transition: all var(--duration-fast);
  }

  .mode-switch button small {
    font-size: 10px;
    color: var(--text-faint);
    margin-left: auto;
  }

  .mode-switch button.active {
    background: #f1f6ff;
    color: #245fdd;
    border-color: #e1ebff;
    font-weight: 700;
  }

  .mode-icon {
    font-size: 16px;
  }

  .compare-icon {
    font-size: 19px;
  }

  .workspace-grid {
    display: grid;
    grid-template-columns: minmax(0,1.55fr) minmax(250px,.78fr);
    gap: var(--space-24);
  }

  .input-column {
    min-width: 0;
  }

  .model-select-wrap {
    margin-top: var(--space-4);
  }

  .select-shell {
    position: relative;
  }

  .select-shell select {
    width: 100%;
    height: 43px;
    padding: 0 40px 0 13px;
    appearance: none;
    border: 1px solid var(--border-control);
    border-radius: var(--radius-sm);
    background: var(--surface-card);
    color: #313b4a;
    outline: none;
    font-size: 12px;
  }

  .select-shell select:focus {
    border-color: var(--brand-light);
    box-shadow: 0 0 0 3px var(--brand-tint);
  }

  .select-shell select:disabled {
    background: #f7f8fa;
  }

  .select-chevron {
    position: absolute;
    right: 15px;
    top: 10px;
    color: #7a8595;
    font-size: 19px;
    pointer-events: none;
  }

  .models-select-wrap {
    margin-top: var(--space-4);
  }

  .model-picker {
    display: flex;
    gap: var(--space-8);
    flex-wrap: wrap;
  }

  .model-pill {
    display: inline-flex;
    align-items: center;
    gap: var(--space-7);
    min-height: 34px;
    padding: 0 var(--space-10);
    border: 1px solid var(--border-card);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    color: #727d8e;
    font-size: 11px;
    transition: all var(--duration-fast);
  }

  .model-pill:hover:not(:disabled) {
    border-color: #b3caff;
  }

  .model-pill.chosen {
    border-color: #d5e3ff;
    background: #f2f7ff;
    color: #2c63d0;
  }

  .model-pill:disabled {
    cursor: not-allowed;
    opacity: .57;
  }

  .pill-check {
    width: 14px;
    height: 14px;
    border: 1px solid #c7ced8;
    border-radius: var(--radius-circle);
    display: grid;
    place-items: center;
    font-size: 10px;
  }

  .chosen .pill-check {
    border-color: var(--brand-strong);
    background: var(--brand-strong);
    color: var(--text-on-brand);
  }

  .pill-lock {
    font-size: 9px;
    color: #a37d3a;
  }

  .model-picker-loading {
    padding: var(--space-12);
    color: #8a93a0;
    font-size: 11px;
  }

  .llm-consent {
    display: flex;
    align-items: center;
    gap: var(--space-10);
    padding: var(--space-12);
    margin-top: 18px;
    border: 1px solid #f1e8d4;
    border-radius: 13px;
    background: #fffdf8;
    cursor: pointer;
    transition: all var(--duration-fast);
  }

  .llm-consent input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .llm-consent:hover,.llm-consent-on {
    border-color: #ecd49f;
    background: #fffaf0;
  }

  .custom-check {
    flex: none;
    width: 16px;
    height: 16px;
    display: grid;
    place-items: center;
    border: 1px solid #d2c7ad;
    border-radius: 5px;
    color: var(--text-on-brand);
    font-size: 11px;
  }

  .llm-consent-on .custom-check {
    border-color: var(--warning-strong);
    background: var(--warning-strong);
  }

  .consent-copy {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .consent-copy b {
    font-size: 11px;
    color: #5f4d2d;
  }

  .consent-copy small {
    font-size: 10px;
    color: #a18e6d;
  }

  .consent-tag {
    margin-left: auto;
    color: #b28a43;
    font-size: 10px;
    letter-spacing: .12em;
    font-weight: 700;
  }

  .action-column {
    display: flex;
    flex-direction: column;
    gap: var(--space-15);
  }

  .action-card {
    min-height: 100%;
    padding: var(--space-24) 23px 20px;
    border-radius: var(--radius-3xl);
    background: linear-gradient(145deg,#f4f8ff 0%,#eef3ff 52%,#f4f6ff 100%);
    position: relative;
    overflow: hidden;
  }

  .action-card:after {
    content: '';
    position: absolute;
    width: 170px;
    height: 170px;
    right: -78px;
    top: -76px;
    border: 1px solid #cbdcff;
    border-radius: var(--radius-circle);
    box-shadow: 0 0 0 18px var(--brand-soft),0 0 0 36px #f0f5ff;
  }

  .action-card-kicker {
    position: relative;
    z-index: 1;
    font-size: 9px;
    letter-spacing: .19em;
    color: #6f8fcd;
    font-weight: 700;
  }

  .action-card h3 {
    position: relative;
    z-index: 1;
    margin: var(--space-17) 0 var(--space-8);
    font-size: 24px;
    letter-spacing: -.045em;
    line-height: 1.5;
  }

  .action-card>p {
    position: relative;
    z-index: 1;
    color: #7f8999;
    font-size: 11px;
    line-height: 1.8;
    margin: 0;
  }

  .action-summary {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: var(--space-11);
    margin: 23px 0 18px;
    padding: var(--space-12);
    background: #ffffffa6;
    border-radius: var(--radius-md);
  }

  .summary-icon {
    display: grid;
    place-items: center;
    width: 29px;
    height: 29px;
    background: #e7f0ff;
    border-radius: var(--radius-xs);
    color: var(--brand-strong);
  }

  .action-summary b,.action-summary small {
    display: block;
  }

  .action-summary b {
    font-size: 11px;
  }

  .action-summary small {
    margin-top: var(--space-4);
    color: #929aaa;
    font-size: 9px;
    max-width: 220px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .submit-button {
    position: relative;
    z-index: 1;
    width: 100%;
    height: 45px;
    border: 0;
    border-radius: var(--radius-sm);
    background: var(--brand);
    color: var(--text-on-brand);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-10);
    font-size: 12px;
    font-weight: 700;
    box-shadow: 0 8px 20px #286cff35;
    transition: all var(--duration-fast);
  }

  .submit-button:hover:not(:disabled) {
    background: var(--brand-hover);
    transform: translateY(-1px);
    box-shadow: 0 11px 24px #286cff48;
  }

  .submit-button:disabled {
    cursor: not-allowed;
    opacity: .48;
    box-shadow: none;
  }

  .button-arrow {
    font-size: 17px;
  }

  .button-spinner {
    width: 13px;
    height: 13px;
    border: 2px solid #ffffff70;
    border-top-color: var(--color-white);
    border-radius: var(--radius-circle);
    animation: spin var(--duration-slow) linear infinite;
  }

  .privacy-note {
    position: relative;
    z-index: 1;
    margin-top: var(--space-14);
    display: flex;
    justify-content: center;
    align-items: center;
    gap: var(--space-6);
    color: #98a2b1;
    font-size: 9px;
  }

  .lock-icon {
    font-size: 15px;
  }

  .mini-stat-row {
    display: flex;
    align-items: center;
    justify-content: space-around;
    padding: var(--space-8) var(--space-4);
  }

  .mini-stat-row div {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-4);
  }

  .mini-stat-row b {
    font-size: 16px;
    color: #313d50;
  }

  .mini-stat-row span {
    font-size: 9px;
    color: var(--text-faint);
  }

  .mini-stat-row>i {
    width: 1px;
    height: 22px;
    background: #e4e9f0;
  }

  .inline-alert {
    display: flex;
    align-items: center;
    gap: var(--space-10);
    padding: var(--space-12) var(--space-14);
    margin-top: var(--space-16);
    border-radius: var(--radius-sm);
    font-size: 11px;
    line-height: 1.6;
  }

  .alert-error {
    background: #fff2f1;
    color: #a74640;
  }

  .alert-warn {
    background: #fff8e9;
    color: #87651e;
  }

  .alert-symbol {
    width: 17px;
    height: 17px;
    flex: none;
    border: 1px solid currentColor;
    border-radius: var(--radius-circle);
    display: grid;
    place-items: center;
    font-size: 11px;
    font-weight: 700;
  }

  .inline-alert button {
    margin-left: auto;
    border: 0;
    background: transparent;
    color: var(--brand);
    font-size: 11px;
    font-weight: 700;
    white-space: nowrap;
  }

  @media(max-width:1050px) {
    .workspace-grid {
      grid-template-columns: minmax(0,1.3fr) minmax(230px,.8fr);
      gap: var(--space-17);
    }
    .studio-card {
      padding: 22px;
    }
  }

  @media(max-width:760px) {
    .studio-card {
      padding: var(--space-17) var(--space-15);
      border-radius: 19px;
    }
    .studio-topline {
      align-items: flex-start;
      gap: var(--space-12);
    }
    .studio-label {
      font-size: 12px;
    }
    .label-en {
      display: none;
    }
    .studio-live {
      font-size: 9px;
      text-align: right;
    }
    .mode-switch {
      padding: var(--space-14) 0 18px;
      gap: var(--space-5);
    }
    .mode-switch button {
      min-width: 0;
      flex: 1;
      padding: var(--space-10) var(--space-8);
      font-size: 11px;
      flex-wrap: wrap;
    }
    .mode-switch button small {
      width: 100%;
      margin-left: 22px;
      margin-top: -3px;
    }
    .workspace-grid {
      grid-template-columns: 1fr;
      gap: var(--space-17);
    }
    .action-card {
      min-height: 0;
      padding: 19px;
    }
    .action-card h3 {
      font-size: 20px;
      margin-top: var(--space-12);
    }
    .action-summary {
      margin: var(--space-16) 0 var(--space-12);
    }
    .privacy-note {
      font-size: 8px;
    }
    .mini-stat-row {
      padding: 0 0 2px;
    }
  }

  @media(max-width:390px) {
    .studio-live {
      max-width: 112px;
    }
    .model-pill {
      font-size: 10px;
    }
  }
</style>
