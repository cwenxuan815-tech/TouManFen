<script setup lang="ts">
/**
 * F6：单模型结果与可信度呈现。
 *
 * 红线：分数均为未校准原始分数；无分数不伪造；拒识仍保留原预测；
 * 人工反馈参考只做并列提示，不替换 pred_class。
 */
import { computed } from 'vue'
import type { ClassifyResponse, ModelInfo, TopKEntry } from '../../types'
import { categoryInfo, modelLabel } from '../shared/meta'
import { categoryWords, categoryWordsMeta } from '../../data/categoryWords'
import FeedbackBar from '../placeholders/FeedbackBar.vue'
import CopyButton from '../common/CopyButton.vue'

const props = defineProps<{
  single: ClassifyResponse
  title: string
  models: ModelInfo[]
}>()

const category = computed(() => categoryInfo(props.single.pred_class))
const toneClass = computed(() => `tone-${category.value.color}`)
const modelName = computed(() => modelLabel(props.models, props.single.model_id))
const modelVersion = computed(() => props.single.model_version || '未提供')

const scoreEntries = computed<TopKEntry[]>(() => {
  const top = props.single.top_k
  if (top && top.length) {
    return [...top].sort((left, right) => right.score - left.score).slice(0, 3)
  }
  const scores = props.single.scores
  if (!scores) return []
  return Object.entries(scores)
    .map(([class_id, score]) => ({ class_id, score: Number(score) }))
    .filter((entry) => Number.isFinite(entry.score))
    .sort((left, right) => right.score - left.score)
    .slice(0, 3)
})

const hasScores = computed(
  () => Boolean(props.single.score_kind) && scoreEntries.value.length > 0,
)
const maxScore = computed(() => scoreEntries.value[0]?.score ?? 0)
const ringPercent = computed(() => `${Math.max(0, Math.min(100, maxScore.value * 100))}%`)

const rejectedReason = computed(() => {
  if (props.single.reject_reason === 'low_max_score') return '最高原始分低于拒识阈值'
  if (props.single.reject_reason === 'high_entropy') return '分数分布较分散（entropy 偏高）'
  return '触发软拒识阈值'
})

const thresholdLine = computed(() => {
  const meta = props.single.threshold_meta
  if (!meta) return ''
  const parts = [`来源：${meta.source === 'synthetic_negatives' ? '合成负样本' : meta.source}`]
  parts.push('分数未校准')
  if (meta.max_score_threshold !== null && meta.max_score_threshold !== undefined) {
    parts.push(`最高分阈值 ${meta.max_score_threshold}`)
  }
  if (meta.entropy_threshold !== null && meta.entropy_threshold !== undefined) {
    parts.push(`entropy 阈值 ${meta.entropy_threshold}`)
  }
  if (meta.scope_note) parts.push(meta.scope_note)
  parts.push('适用范围有限')
  return parts.join(' · ')
})

const words = computed(() => categoryWords[props.single.pred_class] || [])
const hintLabel = computed(() =>
  props.single.human_feedback_hint
    ? categoryInfo(props.single.human_feedback_hint.corrected_class).zh
    : '',
)

const copyText = computed(() =>
  [
    `标题：${props.title}`,
    `模型：${modelName.value}`,
    `版本：${modelVersion.value}`,
    `预测：${category.value.zh}（${props.single.pred_class}）`,
    props.single.rejected ? '提示：可能是非新闻内容（软拒识，预测仍保留）' : '',
    props.single.human_feedback_hint
      ? `人工反馈参考（未审核）：${hintLabel.value}`
      : '',
    hasScores.value ? '分数：未校准原始分数' : '分数：该模型不提供可信度',
    `延迟：${props.single.latency_ms} ms`,
  ]
    .filter(Boolean)
    .join('\n'),
)

function scorePercent(score: number): string {
  return `${Math.max(0, Math.min(100, score * 100)).toFixed(1)}%`
}
</script>

<template>
  <div class="result-card-wrap" data-result-card>
    <div v-if="single.rejected" class="reject-banner" role="status">
      <div class="reject-title"><span aria-hidden="true">!</span> 可能是非新闻内容</div>
      <p>已触发软拒识：{{ rejectedReason }}。本次分类仍有效，下面仍展示模型原始预测。</p>
      <p class="reject-threshold">{{ thresholdLine }}</p>
    </div>

    <div class="single-result-card" :class="toneClass">
      <div class="result-category-mark">{{ category.zh.slice(0, 1) }}</div>
      <div class="result-main">
        <div class="result-kicker">{{ single.pred_class }}</div>
        <div class="result-category-name">{{ category.zh }}</div>
        <div class="result-category-hint">{{ category.hint }}</div>
        <div class="result-context">{{ title }}</div>
        <div class="result-badge-row">
          <span v-if="single.rejected" class="result-pill reject-pill">软拒识 · 预测仍保留</span>
          <span v-if="single.human_feedback_hint" class="result-pill hint-pill">人工反馈参考（未审核）：{{ hintLabel }}</span>
        </div>
      </div>
      <div class="result-meta">
        <span>由 {{ modelName }} 判断</span>
        <b>{{ single.latency_ms }}<small> ms</small></b>
        <span>模型版本 · {{ modelVersion }}</span>
      </div>
    </div>

    <div class="credibility-grid">
      <section v-if="hasScores" class="credibility-card" aria-label="未校准原始分数">
        <div class="credibility-head">
          <div>
            <b>未校准原始分数</b>
            <span>仅作模型原始输出展示，不是校准概率，也不代表真实置信度</span>
          </div>
          <div class="confidence-ring" :style="{ '--ring-percent': ringPercent }">
            <span>{{ maxScore.toFixed(2) }}</span>
            <small>最高分</small>
          </div>
        </div>
        <div class="score-bars">
          <div v-for="entry in scoreEntries" :key="entry.class_id" class="score-row">
            <span class="score-name">{{ categoryInfo(entry.class_id).zh }}</span>
            <div class="score-track" aria-hidden="true">
              <i :style="{ width: `${Math.max(0, Math.min(100, entry.score * 100))}%` }"></i>
            </div>
            <span class="score-value">{{ scorePercent(entry.score) }}</span>
          </div>
        </div>
        <p class="credibility-note">分数来源：{{ single.score_kind }}；跨模型不可比较绝对值。</p>
      </section>

      <section v-else class="credibility-card no-credibility" aria-label="可信度缺失">
        <b>该模型不提供可信度</b>
        <p>本次响应没有通过校验的原始分数；不绘制置信环、不推断概率。</p>
      </section>

      <section class="words-card" aria-label="类别典型词">
        <div class="words-head">
          <b>类别典型词</b>
          <span>{{ categoryWordsMeta.note }}</span>
        </div>
        <div v-if="words.length" class="word-chips">
          <span v-for="word in words" :key="word">{{ word }}</span>
        </div>
        <p v-else class="words-missing">典型词来源未接入，暂不展示。</p>
        <p class="words-source">来源：{{ categoryWordsMeta.source }}（抽样 {{ categoryWordsMeta.sample_size }} 行，词频统计；相关词，非因果）</p>
      </section>
    </div>

    <p v-if="!single.rejected && thresholdLine" class="threshold-footnote">拒识阈值：{{ thresholdLine }}</p>

    <div v-if="single.human_feedback_hint" class="hint-banner" role="note">
      人工反馈参考（未审核）：<b>{{ hintLabel }}</b>。保留模型原预测“{{ category.zh }}”，反馈不会自动改变模型。
    </div>

    <div class="result-actions">
      <CopyButton :text="copyText" label="复制结果" />
      <FeedbackBar
        :title="title"
        :model-id="single.model_id"
        :pred-class="single.pred_class"
        :score="hasScores ? maxScore : null"
      />
    </div>
  </div>
</template>

<style scoped>
.result-card-wrap {
  display: flex;
  flex-direction: column;
  gap: 13px;
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
  color: #193460;
}
.tone-violet { background: var(--cat-violet-bg); }
.tone-cyan { background: var(--cat-cyan-bg); }
.tone-amber { background: var(--cat-amber-bg); }
.tone-indigo { background: var(--cat-indigo-bg); }
.tone-green { background: var(--cat-green-bg); }
.tone-red { background: var(--cat-red-bg); }
.tone-orange { background: var(--cat-orange-bg); }
.tone-pink { background: var(--cat-pink-bg); }
.tone-purple { background: var(--cat-purple-bg); }
.tone-blue { background: var(--cat-blue-bg); }
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
}
.tone-violet .result-category-mark { color: var(--cat-violet-mark); }
.tone-cyan .result-category-mark { color: var(--cat-cyan-mark); }
.tone-amber .result-category-mark { color: var(--cat-amber-mark); }
.tone-indigo .result-category-mark { color: var(--cat-indigo-mark); }
.tone-green .result-category-mark { color: var(--cat-green-mark); }
.tone-red .result-category-mark { color: var(--cat-red-mark); }
.tone-orange .result-category-mark { color: var(--cat-orange-mark); }
.tone-pink .result-category-mark { color: var(--cat-pink-mark); }
.tone-purple .result-category-mark { color: var(--cat-purple-mark); }
.result-main {
  position: relative;
  z-index: 1;
  min-width: 0;
}
.result-kicker {
  color: #7185a4;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.result-category-name {
  font-size: 45px;
  line-height: 1.25;
  font-weight: 700;
  letter-spacing: -0.05em;
  margin-top: 3px;
}
.result-context {
  max-width: 480px;
  margin-top: 5px;
  color: #5b6c85;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.result-category-hint {
  margin-top: 4px;
  color: #7587a1;
  font-size: 10px;
}
.result-badge-row {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 10px;
}
.result-pill {
  display: inline-flex;
  padding: 4px 9px;
  border-radius: var(--radius-pill);
  background: #ffffffb8;
  color: #52657e;
  font-size: 9px;
}
.reject-pill {
  background: #fff0e8;
  color: #a9542b;
}
.hint-pill {
  background: #fff8e6;
  color: #8b6b20;
}
.result-meta {
  position: relative;
  z-index: 1;
  margin-left: auto;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 5px;
  flex: none;
  color: #5d708c;
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
.credibility-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(220px, 0.65fr);
  gap: 13px;
}
.credibility-card,
.words-card {
  min-width: 0;
  padding: 16px 18px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-xl);
  background: var(--surface-card);
}
.credibility-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.credibility-head b {
  display: block;
  font-size: 11px;
  color: var(--text-primary);
  margin-bottom: 3px;
}
.credibility-head span {
  color: var(--text-quiet);
  font-size: 9px;
  line-height: 1.5;
}
.confidence-ring {
  --ring-percent: 0%;
  position: relative;
  width: 64px;
  height: 64px;
  flex: none;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: conic-gradient(var(--brand) var(--ring-percent), var(--brand-tint) 0);
}
.confidence-ring::after {
  content: '';
  position: absolute;
  inset: 7px;
  border-radius: 50%;
  background: var(--surface-card);
}
.confidence-ring span,
.confidence-ring small {
  position: relative;
  z-index: 1;
}
.confidence-ring span {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.confidence-ring small {
  font-size: 8px;
  color: var(--text-quiet);
}
.score-bars {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 12px;
}
.score-row {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) 52px;
  align-items: center;
  gap: 8px;
}
.score-name {
  font-size: 10px;
  color: var(--text-secondary);
}
.score-track {
  height: 7px;
  border-radius: var(--radius-pill);
  background: var(--surface-page);
  overflow: hidden;
}
.score-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--brand);
  transition: width var(--duration-slow) ease;
}
.score-value {
  text-align: right;
  color: var(--text-quiet);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}
/* F14：减少动效时，分数条宽度动画直接跳到终值。 */
@media (prefers-reduced-motion: reduce) {
  .score-track i {
    transition: none;
  }
}
.credibility-note,
.words-source {
  margin: 11px 0 0;
  color: var(--text-faint);
  font-size: 9px;
  line-height: 1.5;
}
.no-credibility b {
  display: block;
  margin-bottom: 5px;
  color: var(--text-secondary);
  font-size: 11px;
}
.no-credibility p {
  margin: 0;
  color: var(--text-quiet);
  font-size: 10px;
  line-height: 1.6;
}
.words-head b {
  display: block;
  margin-bottom: 3px;
  color: var(--text-primary);
  font-size: 11px;
}
.words-head span {
  color: var(--text-quiet);
  font-size: 9px;
}
.word-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.word-chips span {
  padding: 4px 8px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-pill);
  background: var(--surface-page);
  color: var(--text-secondary);
  font-size: 9px;
}
.words-missing {
  margin: 9px 0 0;
  color: var(--text-quiet);
  font-size: 10px;
}
.reject-banner {
  padding: 12px 15px;
  border: 1px solid var(--warning-border);
  border-left: 3px solid var(--warning-text);
  border-radius: var(--radius-md);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 10px;
  line-height: 1.6;
}
.reject-title {
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 3px;
}
.reject-banner p {
  margin: 2px 0;
}
.reject-threshold {
  color: var(--text-quiet);
  font-size: 9px;
}
.threshold-footnote {
  margin: 0;
  color: var(--text-faint);
  font-size: 9px;
}
.hint-banner {
  padding: 9px 12px;
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-md);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 10px;
}
.result-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
@media (max-width: 760px) {
  .single-result-card {
    min-height: 190px;
    padding: var(--space-24) 20px;
    gap: var(--space-16);
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .result-category-mark {
    width: 54px;
    height: 54px;
    border-radius: 18px;
    font-size: 28px;
  }
  .result-category-name {
    font-size: 30px;
  }
  .result-context {
    max-width: 100%;
    white-space: normal;
  }
  .result-meta {
    margin-left: 0;
    align-items: flex-start;
    width: 100%;
    flex-direction: row;
    flex-wrap: wrap;
  }
  .credibility-grid {
    grid-template-columns: 1fr;
  }
}
</style>
