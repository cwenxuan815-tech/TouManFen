/** 页面共享的类别/模型展示元数据与纯展示函数（非 composable）。 */
import type { ModelInfo } from '../../types'

export type CategoryColor =
  | 'blue'
  | 'violet'
  | 'cyan'
  | 'amber'
  | 'indigo'
  | 'green'
  | 'red'
  | 'orange'
  | 'pink'
  | 'purple'

export interface CategoryMeta {
  zh: string
  hint: string
  color: CategoryColor
}

export const categoryNames: Record<string, CategoryMeta> = {
  finance: { zh: '财经', hint: '宏观经济 · 银行金融', color: 'violet' },
  realty: { zh: '房产', hint: '楼市 · 地产动态', color: 'blue' },
  stocks: { zh: '股票', hint: '证券市场 · 股价走势', color: 'cyan' },
  education: { zh: '教育', hint: '校园 · 教育政策', color: 'amber' },
  science: { zh: '科技', hint: '科学发现 · 技术创新', color: 'indigo' },
  society: { zh: '社会', hint: '民生 · 社会事件', color: 'green' },
  politics: { zh: '政治', hint: '政务 · 公共政策', color: 'red' },
  sports: { zh: '体育', hint: '赛事 · 运动人物', color: 'orange' },
  game: { zh: '游戏', hint: '主机 · 电竞 · 手游', color: 'pink' },
  entertainment: { zh: '娱乐', hint: '影视 · 音乐 · 明星', color: 'purple' },
}

export function categoryInfo(id: string): CategoryMeta {
  return categoryNames[id] || { zh: id, hint: '模型分类结果', color: 'blue' }
}

/** 示例标题（保持与拆分前一致）。 */
export const exampleTitles = [
  '北京二手房成交量创年内新高',
  '新一代人工智能芯片发布，算力提升显著',
  '中国女篮公布亚洲杯参赛名单',
]

/** 页面标题上限（与 F1 前交互一致）。 */
export const MAX_TITLE_CHARS = 200

export function modelLabel(models: ModelInfo[], id: string): string {
  return models.find((item) => item.model_id === id)?.display_name || id
}

export type ModelStatus = 'up' | 'down' | 'disabled' | 'unknown'

export function modelStatus(model: ModelInfo): ModelStatus {
  if (!model.enabled) return 'disabled'
  return model.status === 'up' ? 'up' : model.status === 'down' ? 'down' : 'unknown'
}

export function statusText(status: ModelStatus): string {
  if (status === 'up') return '服务就绪'
  if (status === 'down') return '暂不可用'
  if (status === 'disabled') return '未启用'
  return '状态未知'
}
