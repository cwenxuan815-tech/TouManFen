<script setup lang="ts">
/**
 * F10：竞技场人类偏好榜（B7 `/arena/leaderboard`）。
 *
 * 匿名性约束（Lead 补充）：投票前**不请求**榜单接口，本组件在
 * `available === false` 时只渲染占位与规则说明，不出现任何真实模型名。
 * 首次投票成功后才由页面传入榜单数据。
 *
 * 展示规则（§4.3）：比较样本数 < min_votes_for_ranking 时显示「预热中」+
 * Wilson 95% 区间，不显示名次；`status === 'ready'` 才显示名次。
 * `votes` 是用户选中次数，`sample_size` 是比较样本数。固定说明
 * 「人类偏好投票强度，仅供演示参考」，不得表述为准确率/客观排名。
 */
import { computed } from 'vue'
import type { ArenaLeaderboardResponse } from '../../types'
import AsyncState from '../common/AsyncState.vue'

const props = defineProps<{
  leaderboard: ArenaLeaderboardResponse | null
  available: boolean
  loading: boolean
  error: string
  requestId?: string
}>()

const emit = defineEmits<{ retry: [] }>()

const ready = computed(() => props.leaderboard?.status === 'ready')
const minVotes = computed(() => props.leaderboard?.min_votes_for_ranking ?? 50)
const statusLabel = computed(() => (ready.value ? '已就绪' : '预热中'))
const rows = computed(() => {
  const entries = props.leaderboard?.entries ?? []
  if (!ready.value) return entries
  return [...entries].sort((a, b) => (a.rank ?? Number.MAX_SAFE_INTEGER) - (b.rank ?? Number.MAX_SAFE_INTEGER))
})

// wins 在旧版网关中就是 vote.model_id 的计数；新版 votes 与其同义。
// 读取 wins 可兼容用户尚未重启的旧网关，避免把总样本数显示成获选票数。
function selectedVotes(entry: (typeof rows.value)[number]): number {
  return Number.isFinite(entry.wins) ? entry.wins : entry.votes
}

function sampleSize(entry: (typeof rows.value)[number]): number {
  return Number.isFinite(entry.sample_size) ? Number(entry.sample_size) : entry.votes
}

function pct(value: number): string {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return `${(clamped * 100).toFixed(1)}%`
}

function intervalText(lower: number, upper: number): string {
  return `${pct(lower)} – ${pct(upper)}`
}
</script>

<template>
  <article
    class="arena-board"
    data-arena-leaderboard
    :data-arena-board-status="leaderboard?.status ?? 'idle'"
    :data-arena-board-available="available ? 'true' : 'false'"
    aria-labelledby="arena-board-title"
  >
    <header class="arena-board-head">
      <div>
        <div class="arena-board-kicker">HUMAN PREFERENCE</div>
        <h3 id="arena-board-title">人类偏好榜</h3>
      </div>
      <span class="arena-board-badge" :class="{ 'is-ready': ready }">{{ statusLabel }}</span>
    </header>

    <p class="arena-board-disclaimer" data-arena-disclaimer>
      人类偏好投票强度，仅供演示参考
    </p>

    <AsyncState
      v-if="!available"
      state="idle"
      title="投票后展示人类偏好榜"
      :message="`Elo 与票数全部来自真实投票记录；票数不足 ${minVotes} 时只显示「预热中」与 Wilson 95% 区间，不显示名次。`"
      data-arena-board-placeholder
    />

    <AsyncState
      v-else-if="loading && !leaderboard"
      state="loading"
      title="正在读取榜单…"
    />

    <AsyncState
      v-else-if="error"
      state="error"
      :message="error"
      :request-id="requestId"
      retry-label="重新读取"
      data-arena-board-error
      @retry="emit('retry')"
    />

    <AsyncState v-else-if="!leaderboard" state="idle" title="榜单暂未就绪" />

    <template v-else>
      <p class="arena-board-rule" data-arena-board-rule>
        <template v-if="ready">所有模型参与的比较样本均达到 {{ minVotes }}，显示正式名次。</template>
        <template v-else>比较样本不足 {{ minVotes }}：仅显示预热态与 Wilson 95% 区间，不显示名次。</template>
      </p>

      <table class="arena-board-table">
        <caption class="arena-board-caption">人类偏好投票榜（vote）</caption>
        <thead>
          <tr>
            <th v-if="ready" scope="col" class="arena-col-rank">名次</th>
            <th scope="col">模型</th>
            <th scope="col" class="arena-col-num">获选票 / 样本 n</th>
            <th scope="col" class="arena-col-num">Elo</th>
            <th scope="col" class="arena-col-wilson">Wilson 95% 区间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in rows"
            :key="entry.model_id"
            data-arena-entry
            :data-model-id="entry.model_id"
            :data-display-name="entry.display_name"
            :data-votes="selectedVotes(entry)"
            :data-sample-size="sampleSize(entry)"
            :data-elo="entry.elo"
            :data-wilson-lower="entry.wilson_lower"
            :data-wilson-upper="entry.wilson_upper"
            :data-warming-up="entry.warming_up ? 'true' : 'false'"
            :data-rank="ready && entry.rank !== null ? entry.rank : null"
          >
            <td v-if="ready" class="arena-rank" data-label="名次">{{ entry.rank ?? '—' }}</td>
            <td class="arena-model-cell" data-label="模型">
              <b>{{ entry.display_name }}</b>
              <small v-if="entry.warming_up" class="arena-warmup-tag">预热中</small>
            </td>
            <td class="arena-col-num arena-vote-count" data-label="获选票 / 样本 n">
              <b>{{ selectedVotes(entry) }}</b><small> / {{ sampleSize(entry) }}</small>
            </td>
            <td class="arena-col-num arena-elo" data-label="Elo">{{ Math.round(entry.elo) }}</td>
            <td class="arena-col-wilson arena-wilson" data-label="Wilson 95%">
              {{ intervalText(entry.wilson_lower, entry.wilson_upper) }}
            </td>
          </tr>
        </tbody>
      </table>

      <p v-if="leaderboard.note" class="arena-board-note" data-arena-board-note>
        {{ leaderboard.note }}
      </p>
      <p class="arena-board-note">获选票是用户选中该模型的次数；样本 n 是该模型参与投票题的次数，Wilson 区间按样本 n 计算。</p>
    </template>
  </article>
</template>

<style scoped>
.arena-board {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
  padding: var(--space-17);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-3xl);
  background: var(--surface-card);
}

.arena-board-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-10);
}

.arena-board-kicker {
  font-size: 9px;
  letter-spacing: 0.18em;
  color: var(--text-faint);
  font-weight: 700;
}

.arena-board h3 {
  margin: var(--space-5) 0 0;
  font-size: 19px;
  letter-spacing: -0.03em;
}

.arena-board-badge {
  flex: none;
  padding: 3px var(--space-9);
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-pill);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 10px;
  font-weight: 700;
}

.arena-board-badge.is-ready {
  border-color: var(--success-soft);
  background: var(--success-soft);
  color: var(--success-text);
}

.arena-board-disclaimer {
  margin: 0;
  padding: var(--space-8) var(--space-10);
  border-radius: var(--radius-sm);
  background: var(--surface-page);
  color: var(--text-secondary);
  font-size: 10px;
}

.arena-board-rule {
  margin: 0;
  color: var(--text-quiet);
  font-size: 10px;
  line-height: 1.7;
}

.arena-board-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.arena-board-caption {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.arena-board-table th {
  padding: var(--space-7) var(--space-8);
  border-bottom: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 9px;
  font-weight: 600;
  text-align: right;
  white-space: nowrap;
}

.arena-board-table th:nth-child(1),
.arena-board-table th:nth-child(2) {
  text-align: left;
}

.arena-board-table td {
  padding: var(--space-8);
  border-bottom: 1px solid var(--border);
  text-align: right;
}

.arena-board-table td:nth-child(1):not(.arena-rank) {
  text-align: left;
}

.arena-board-table tbody tr:last-child td {
  border-bottom: 0;
}

.arena-col-num {
  width: 56px;
}

.arena-col-wilson {
  width: 128px;
}

.arena-col-rank {
  width: 42px;
}

.arena-rank {
  color: var(--brand);
  font-weight: 700;
  text-align: left;
}

.arena-model-cell {
  text-align: left;
}

.arena-model-cell b {
  font-size: 12px;
  color: var(--text-primary);
}

.arena-warmup-tag {
  display: inline-block;
  margin-left: var(--space-6);
  padding: 1px var(--space-6);
  border-radius: var(--radius-pill);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 9px;
}

.arena-elo {
  font-weight: 700;
  color: var(--text-primary);
}

.arena-vote-count b {
  color: var(--text-primary);
  font-size: 14px;
}

.arena-vote-count small {
  color: var(--text-faint);
  font-size: 12px;
}

.arena-wilson {
  color: var(--text-secondary);
}

.arena-board-note {
  margin: 0;
  color: var(--text-subtle);
  font-size: 9px;
  line-height: 1.6;
}

@media (max-width: 760px) {
  .arena-board {
    padding: var(--space-14);
  }

  .arena-board-table,
  .arena-board-table tbody {
    display: block;
  }

  .arena-board-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .arena-board-table tbody tr {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    grid-template-areas:
      'model votes elo'
      'model wilson wilson';
    align-items: center;
    gap: 4px 10px;
    padding: 10px 4px;
    border-bottom: 1px solid var(--border);
  }

  .arena-board-table tbody tr:last-child {
    border-bottom: 0;
  }

  .arena-board-table tbody tr[data-rank] {
    grid-template-columns: 24px minmax(0, 1fr) auto auto;
    grid-template-areas:
      'rank model votes elo'
      'rank model wilson wilson';
  }

  .arena-board-table td {
    width: auto;
    padding: 0;
    border: 0;
    text-align: right;
    white-space: nowrap;
  }

  .arena-model-cell {
    grid-area: model;
    white-space: normal !important;
  }

  .arena-rank {
    display: none;
  }

  .arena-board-table tbody tr[data-rank] .arena-rank {
    display: block;
    grid-area: rank;
  }

  .arena-vote-count {
    grid-area: votes;
  }

  .arena-elo {
    grid-area: elo;
  }

  .arena-wilson {
    grid-area: wilson;
  }

  .arena-vote-count::before,
  .arena-elo::before,
  .arena-wilson::before {
    margin-right: 4px;
    color: var(--text-faint);
    font-size: 10px;
    font-weight: 400;
  }

  .arena-vote-count::before {
    content: '获选';
  }

  .arena-elo::before {
    content: 'Elo';
  }

  .arena-wilson::before {
    content: 'Wilson';
  }

  .arena-model-cell b {
    overflow-wrap: anywhere;
  }

  .arena-vote-count b {
    font-size: 14px;
  }

  .arena-vote-count small {
    font-size: 12px;
  }

  .arena-board-note {
    overflow-wrap: anywhere;
  }
}
</style>
