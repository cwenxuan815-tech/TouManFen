<script setup lang="ts">
/**
 * F10：竞技场页面区块（`<section id="arena">`）。
 *
 * 组成：匿名候选卡片 A~E（翻牌揭晓）+ 人类偏好榜（预热态/正式名次）。
 * 复用 F3 `useArena` / F4 `AsyncState`、`Toaster`，不自写错误横幅、不新增依赖。
 *
 * 匿名性约束（Lead 补充）：
 * - 投票前只请求 `/arena/next`，**不请求** `/arena/leaderboard`；
 * - 投票前页面 DOM / 请求载荷都不出现 `model_id` 或真实模型名；
 * - 首次投票成功后才拉取榜单；换下一题时榜单回到占位（数据不重新请求）。
 *
 * 文案红线：Elo 只表示人类偏好强度，不得写成准确率或客观排名。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useArena } from '../../composables/useArena'
import type { ArenaVoteResponse } from '../../types'
import { categoryInfo } from '../shared/meta'
import AsyncState from '../common/AsyncState.vue'
import { pushToast } from '../common/toast'
import ArenaAnonymousCard from '../arena/ArenaAnonymousCard.vue'
import ArenaLeaderboard from '../arena/ArenaLeaderboard.vue'

const LETTERS = ['A', 'B', 'C', 'D'] as const

const {
  item,
  leaderboard,
  voteResult,
  selectedAnonId,
  nextLoading,
  voteLoading,
  leaderboardLoading,
  nextError,
  nextErrorCode,
  nextRequestId,
  voteError,
  voteErrorCode,
  voteRequestId,
  leaderboardError,
  leaderboardRequestId,
  canVote,
  loadNext,
  vote,
  loadLeaderboard,
} = useArena()

/** 服务端 candidates 顺序 → 本地字母锚点（A/B/C/D）；不参与任何模型身份推断。 */
const candidates = computed(() =>
  (item.value?.candidates ?? []).map((candidate, index) => ({
    candidate,
    letter: LETTERS[index] ?? String(index + 1),
  })),
)
/** 正常为 4 列；partial 候选更少时仍等间距铺满，不留下空列。 */
const gridColumns = computed(() => Math.max(1, Math.min(4, candidates.value.length || 4)))

const loadPhase = computed<'loading' | 'empty' | 'error' | 'ready'>(() => {
  if (nextLoading.value) return 'loading'
  if (nextErrorCode.value === 'ARENA_NO_ITEMS') return 'empty'
  if (nextError.value) return 'error'
  return item.value ? 'ready' : 'loading'
})

const hasVoted = computed(() => voteResult.value !== null)
/** 榜单仅在“当前题目已投票揭晓”后加载/展示（匿名性硬约束）。 */
const boardAvailable = computed(() => hasVoted.value)
const pageState = computed(() => (loadPhase.value === 'ready' ? (hasVoted.value ? 'revealed' : 'ready') : loadPhase.value))
const nextLabel = computed(() => (hasVoted.value ? '下一题' : '换一题'))
const revealAnnouncement = computed(() => {
  const result = voteResult.value
  if (!result) return ''
  const zh = categoryInfo(result.selected.pred_class).zh
  return `已揭晓：${result.selected.display_name}（${result.selected.model_id}），预测类别 ${zh}`
})

/** F14：投票后卡片变为 disabled，焦点会掉回 body；改落到揭晓状态区。 */
const revealRef = ref<HTMLElement | null>(null)

function onVote(anonId: string): void {
  void vote(anonId)
}

function onNext(): void {
  void loadNext()
}

function onReloadLeaderboard(): void {
  void loadLeaderboard()
}

// 投票失败（重复/限速/网络）统一走 F4 Toaster；不伪造“已投票”。
watch([voteError, voteErrorCode, voteRequestId], () => {
  if (!voteError.value) return
  const code = voteErrorCode.value
  const duplicate = code === 'ARENA_DUPLICATE_VOTE'
  const rateLimited = code === 'ARENA_RATE_LIMITED'
  pushToast({
    kind: duplicate || rateLimited ? 'warning' : 'error',
    message: voteError.value,
    detail: duplicate
      ? '同一道题每个客户端只计 1 票；可以换一题继续。'
      : rateLimited
        ? '单客户端每分钟最多 20 票，请稍后再试。'
        : undefined,
    requestId: voteRequestId.value || undefined,
  })
})

watch(voteResult, (result: ArenaVoteResponse | null) => {
  if (!result) return
  pushToast({
    kind: 'success',
    message: '投票已记录，已揭晓你选择的模型。',
    requestId: result.request_id,
  })
  // F14：只在该焦点因卡片禁用而丢失时接管，不打断鼠标用户当前焦点。
  void nextTick(() => {
    const active = document.activeElement as HTMLElement | null
    const candidateGrid = document.querySelector('[data-arena-candidates]')
    if (
      !active ||
      active === document.body ||
      (candidateGrid && candidateGrid.contains(active))
    ) {
      revealRef.value?.focus()
    }
  })
})

onMounted(() => {
  // 仅抽题；榜单在首次投票成功后再请求。
  void loadNext()
})
</script>

<template>
  <section
    id="arena"
    class="lab-section arena-section"
    aria-labelledby="arena-title"
    :data-arena-page-state="pageState"
  >
    <div class="section-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> PREFERENCE ARENA</div>
        <h2 id="arena-title">匿名投票，偏好可读。</h2>
      </div>
      <p>
        从 4 个匿名结果里选出更像正确标签的一个，<br class="desktop-break" />
        投票后翻牌揭晓你选中的模型。
      </p>
    </div>

    <div class="arena-grid">
      <div class="arena-panel">
        <div class="arena-panel-head">
          <div class="arena-panel-title">
            <span class="arena-step">01</span>
            <b>匿名候选</b>
          </div>
          <div class="arena-panel-actions">
            <button
              type="button"
              class="arena-next"
              data-arena-next
              :disabled="nextLoading || voteLoading"
              @click="onNext"
            >
              {{ nextLabel }}
            </button>
          </div>
        </div>

        <p
          v-if="item"
          class="arena-item-title"
          data-arena-item-title
          :title="item.title"
        >{{ item.title }}</p>

        <AsyncState
          v-if="loadPhase === 'loading'"
          state="loading"
          title="正在抽取题目…"
          loading-text="正在抽取题目…"
          data-arena-loading
        />

        <template v-else-if="loadPhase === 'empty'">
          <AsyncState
            state="empty"
            title="暂无可用题目"
            message="竞技场题库为空或题目文件不可用；可重新抽取或稍后再试。"
            data-arena-empty
          />
          <button type="button" class="arena-inline-retry" data-arena-empty-retry @click="onNext">
            重新抽取
          </button>
        </template>

        <AsyncState
          v-else-if="loadPhase === 'error'"
          state="error"
          :message="nextError"
          :request-id="nextRequestId"
          retry-label="重新抽取"
          data-arena-load-error
          @retry="onNext"
        />

        <template v-else-if="item">
          <AsyncState
            v-if="item.partial"
            state="partial"
            title="部分候选已返回"
            message="部分模型结果缺失，仅展示可用候选；投票只作用于本次展示的匿名结果。"
          />
          <p v-else class="arena-instruction">
            点击卡片投下偏好票；投票前不会显示任何模型名称。
          </p>

          <div
            class="arena-cards"
            data-arena-candidates
            :data-arena-item-id="item.item_id"
            :style="{ '--arena-columns': gridColumns }"
          >
            <ArenaAnonymousCard
              v-for="entry in candidates"
              :key="entry.candidate.anon_id"
              :letter="entry.letter"
              :candidate="entry.candidate"
              :selected="selectedAnonId === entry.candidate.anon_id"
              :revealed="hasVoted && selectedAnonId === entry.candidate.anon_id"
              :pending="voteLoading && selectedAnonId === entry.candidate.anon_id"
              :muted="hasVoted && selectedAnonId !== entry.candidate.anon_id"
              :disabled="!canVote"
              :result="selectedAnonId === entry.candidate.anon_id ? voteResult : null"
              @vote="onVote(entry.candidate.anon_id)"
            />
          </div>

          <p
            v-if="hasVoted"
            ref="revealRef"
            class="arena-reveal-note"
            data-arena-revealed
            role="status"
            aria-live="polite"
            tabindex="-1"
          >
            {{ revealAnnouncement }}
          </p>

          <p class="arena-rule" data-arena-rule>
            同一题目每个客户端只计 1 票；单客户端每分钟最多 20 票。票数可被本机清除，仅作演示参考。
          </p>

          <div v-if="hasVoted" class="arena-aftervote">
            <button type="button" class="arena-inline-next" data-arena-aftervote-next @click="onNext">
              再抽一题
            </button>
          </div>
        </template>
      </div>

      <ArenaLeaderboard
        :leaderboard="leaderboard"
        :available="boardAvailable"
        :loading="leaderboardLoading"
        :error="leaderboardError"
        :request-id="leaderboardRequestId"
        @retry="onReloadLeaderboard"
      />
    </div>
  </section>
</template>

<style scoped>
.arena-section {
  padding-top: 96px;
  padding-bottom: 72px;
}

.arena-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.42fr) minmax(320px, 0.9fr);
  gap: var(--space-24);
  align-items: start;
}

.arena-panel {
  padding: var(--space-24) var(--space-24) var(--space-17);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-5xl);
  background: var(--surface-card);
  box-shadow: 0 24px 80px #2a3f6010;
}

.arena-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-14);
  padding-bottom: var(--space-16);
  border-bottom: 1px solid var(--border);
}

.arena-panel-title {
  display: flex;
  align-items: center;
  gap: var(--space-10);
  min-width: 0;
}

.arena-step {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-xs);
  background: var(--brand-tint);
  color: var(--brand-strong);
  font-size: 11px;
  font-weight: 700;
}

.arena-panel-title b {
  font-size: 15px;
  white-space: nowrap;
}

.arena-item-title {
  max-width: 100%;
  margin: var(--space-14) 0 0;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  color: var(--text-primary);
  font-size: 16px;
  font-weight: 600;
  line-height: 1.6;
  overflow-wrap: anywhere;
  white-space: normal;
}

.arena-panel-actions {
  display: flex;
  align-items: center;
  gap: var(--space-10);
}

.arena-next,
.arena-inline-next,
.arena-inline-retry {
  min-height: 30px;
  padding: 0 var(--space-12);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 10px;
  font-weight: 700;
  white-space: nowrap;
  transition: border-color var(--duration-fast), color var(--duration-fast);
}

.arena-next:hover:not(:disabled),
.arena-inline-next:hover,
.arena-inline-retry:hover {
  border-color: var(--brand-light);
  color: var(--brand);
}

.arena-next:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.arena-instruction {
  margin: var(--space-14) 0 var(--space-12);
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.7;
}

.arena-cards {
  display: grid;
  grid-template-columns: repeat(var(--arena-columns, 4), minmax(0, 1fr));
  gap: var(--space-10);
  margin: var(--space-14) 0 var(--space-12);
}

.arena-reveal-note {
  margin: var(--space-10) 0 0;
  padding: var(--space-10) var(--space-12);
  border: 1px solid var(--border-card);
  border-left: 3px solid var(--brand);
  border-radius: var(--radius-sm);
  background: var(--brand-tint);
  color: var(--text-primary);
  font-size: 11px;
  line-height: 1.7;
}

.arena-rule {
  margin: var(--space-10) 0 0;
  color: var(--text-faint);
  font-size: 9px;
  line-height: 1.7;
}

.arena-aftervote {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-12);
}

.arena-inline-retry {
  display: block;
  margin: var(--space-10) auto 0;
}

@media (max-width: 1050px) {
  .arena-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .arena-section {
    padding-top: 72px;
  }
}

@media (max-width: 760px) {
  .arena-section {
    padding-top: 56px;
    padding-bottom: 48px;
  }

  .arena-panel {
    padding: var(--space-17) var(--space-15);
    border-radius: 19px;
  }

  .arena-panel-head {
    align-items: flex-start;
    gap: var(--space-10);
  }

  .arena-item-title {
    margin-top: var(--space-12);
    font-size: 14px;
    line-height: 1.55;
  }

  .arena-panel-actions {
    flex-direction: column;
    align-items: flex-end;
  }

  .arena-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .arena-cards :deep(.arena-card) {
    height: 156px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .arena-next,
  .arena-inline-next,
  .arena-inline-retry {
    transition: none;
  }
}
</style>
