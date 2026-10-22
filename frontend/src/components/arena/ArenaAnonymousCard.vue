<script setup lang="ts">
/**
 * F10：竞技场匿名候选卡片（A~E）。
 *
 * 匿名性约束：
 * - 投票前卡片 DOM 只含字母、`pred_class`（类别）与本地 `anon_id` 锚点，
 *   **不渲染、不持有**任何 `model_id` / `display_name`；
 * - 翻牌后的“背面”仅在 `revealed && result` 时通过 `v-if` 挂载，因此投票前
 *   真实模型名与 model_id 根本不存在于 DOM 中（不是靠 CSS 隐藏）；
 * - 翻牌动画约 200ms（`--duration-fast`）；`prefers-reduced-motion: reduce`
 *   时由 CSS 直接跳过动画、立即呈现背面。
 */
import { computed } from 'vue'
import type { ArenaCandidate, ArenaVoteResponse } from '../../types'
import { categoryInfo } from '../shared/meta'

const props = defineProps<{
  /** 服务端 candidates 顺序映射出的字母 A~E。 */
  letter: string
  candidate: ArenaCandidate
  /** 本卡片是否被用户选中（投票中/已投票）。 */
  selected: boolean
  /** 是否已揭晓（仅成功投票后为 true）。 */
  revealed: boolean
  /** 投票请求进行中。 */
  pending: boolean
  /** 已投票后其他卡片置灰。 */
  muted: boolean
  /** 是否禁止点击（投票中/已投票）。 */
  disabled: boolean
  /** 仅被选中的卡片持有成功响应。 */
  result: ArenaVoteResponse | null
}>()

const emit = defineEmits<{ vote: [] }>()

const category = computed(() => categoryInfo(props.candidate.pred_class))
const revealedModelId = computed(() =>
  props.revealed && props.result ? props.result.selected.model_id : '',
)
const revealedName = computed(() =>
  props.revealed && props.result ? props.result.selected.display_name : '',
)
const cardState = computed(() => {
  if (props.revealed) return 'revealed'
  if (props.pending && props.selected) return 'pending'
  if (props.selected) return 'selected'
  return 'idle'
})
const categoryStyle = computed<Record<string, string>>(() => ({
  '--arena-cat-bg': `var(--cat-${category.value.color}-bg)`,
  '--arena-cat-mark': `var(--cat-${category.value.color}-mark)`,
}))
const ariaLabel = computed(() => {
  if (props.revealed) return `已揭晓：${revealedName.value}（${revealedModelId.value}）`
  return `选择匿名候选 ${props.letter}，预测类别 ${category.value.zh}`
})
</script>

<template>
  <button
    type="button"
    class="arena-card"
    :class="{
      'is-selected': selected,
      'is-revealed': revealed,
      'is-pending': pending && selected,
      'is-muted': muted,
    }"
    :data-arena-card="letter"
    :data-anon-letter="letter"
    :data-anon-id="candidate.anon_id"
    :data-card-state="cardState"
    :data-revealed-model-id="revealedModelId || null"
    :disabled="disabled"
    :aria-label="ariaLabel"
    :aria-pressed="selected || undefined"
    @click="emit('vote')"
  >
    <span class="arena-card-inner">
      <span class="arena-card-face arena-card-front" :style="categoryStyle">
        <span class="arena-card-topline">
          <span class="arena-letter">{{ letter }}</span>
          <span class="arena-anon-tag">匿名候选</span>
        </span>
        <span class="arena-pred">{{ category.zh }}</span>
        <span class="arena-pred-id">{{ candidate.pred_class }}</span>
        <span class="arena-card-hint">{{ pending && selected ? '投票中…' : '选择它' }}</span>
      </span>

      <span
        v-if="revealed && result"
        class="arena-card-face arena-card-back"
        data-arena-reveal
        :style="categoryStyle"
      >
        <span class="arena-back-kicker">你选择了</span>
        <span class="arena-model-name" data-arena-revealed-name>{{ result.selected.display_name }}</span>
        <span class="arena-model-id" data-arena-revealed-model-id>{{ result.selected.model_id }}</span>
        <span class="arena-back-pred">{{ categoryInfo(result.selected.pred_class).zh }}</span>
        <span class="arena-back-elo">Elo {{ Math.round(result.elo_after) }}</span>
      </span>
    </span>
  </button>
</template>

<style scoped>
.arena-card {
  position: relative;
  display: block;
  width: 100%;
  height: 172px;
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  perspective: 900px;
  cursor: pointer;
}

.arena-card-inner {
  position: relative;
  display: block;
  width: 100%;
  height: 100%;
  transform-style: preserve-3d;
}

.arena-card.is-revealed .arena-card-inner {
  animation: arena-flip var(--duration-fast) ease-out both;
}

@keyframes arena-flip {
  from {
    transform: rotateY(0deg);
  }
  to {
    transform: rotateY(180deg);
  }
}

.arena-card-face {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-14);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
  backface-visibility: hidden;
  transition: border-color var(--duration-fast), box-shadow var(--duration-fast),
    transform var(--duration-fast);
}

.arena-card-front {
  background: var(--surface-card);
}

.arena-card-topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-6);
}

.arena-letter {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: var(--radius-sm);
  background: var(--arena-cat-bg, var(--brand-tint));
  color: var(--arena-cat-mark, var(--brand));
  font-size: 17px;
  font-weight: 700;
}

.arena-anon-tag {
  font-size: 9px;
  letter-spacing: 0.12em;
  color: var(--text-faint);
}

.arena-pred {
  margin-top: var(--space-8);
  font-size: 19px;
  font-weight: 700;
  color: var(--text-strong);
  letter-spacing: -0.02em;
}

.arena-pred-id {
  font-size: 10px;
  color: var(--text-faint);
  letter-spacing: 0.06em;
}

.arena-card-hint {
  margin-top: auto;
  font-size: 10px;
  color: var(--brand);
  font-weight: 700;
}

.arena-card:hover:not(:disabled) .arena-card-face {
  border-color: var(--brand-light);
  box-shadow: 0 10px 28px #286cff1a;
  transform: translateY(-2px);
}

.arena-card.is-selected .arena-card-front {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-tint);
}

.arena-card.is-pending .arena-card-inner {
  animation: arena-pulse var(--duration-pulse) ease-in-out infinite;
}

@keyframes arena-pulse {
  50% {
    opacity: 0.72;
  }
}

.arena-card:disabled {
  cursor: default;
}

.arena-card.is-muted {
  opacity: 0.5;
}

.arena-card-back {
  transform: rotateY(180deg);
  background: var(--arena-cat-bg, var(--brand-tint));
  border-color: transparent;
  justify-content: center;
}

.arena-back-kicker {
  font-size: 9px;
  letter-spacing: 0.16em;
  color: var(--text-quiet);
}

.arena-model-name {
  font-size: 21px;
  font-weight: 700;
  color: var(--arena-cat-mark, var(--brand-dark));
  letter-spacing: -0.03em;
  line-height: 1.3;
}

.arena-model-id {
  display: inline-block;
  align-self: flex-start;
  margin-top: var(--space-4);
  padding: 1px var(--space-6);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 9px;
  font-family: var(--font-sans);
  letter-spacing: 0.04em;
}

.arena-back-pred {
  margin-top: var(--space-8);
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
}

.arena-back-elo {
  margin-top: var(--space-4);
  font-size: 10px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

@media (prefers-reduced-motion: reduce) {
  .arena-card.is-revealed .arena-card-inner {
    animation: none;
    transform: rotateY(180deg);
  }

  .arena-card.is-pending .arena-card-inner {
    animation: none;
  }

  .arena-card-face {
    transition: none;
  }

  .arena-card:hover:not(:disabled) .arena-card-face {
    transform: none;
  }
}
</style>
