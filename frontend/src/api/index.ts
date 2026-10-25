/** F3：API 客户端统一出口。 */
export {
  API_BASE,
  API_PREFIX,
  ApiClientError,
  TIMEOUT_MS,
  isAbortError,
  isNotImplementedError,
  request,
} from './client'
export type { RequestOptions, TimeoutProfile } from './client'
export {
  COMPARE_STREAM_READY,
  classify,
  classifyBatch,
  classifyRealtime,
  compare,
  compareStreamUrl,
  getModels,
  openCompareStream,
  supportsCompareStream,
} from './classify'
export type {
  CompareStreamConnection,
  CompareStreamDoneHandler,
  CompareStreamErrorHandler,
  CompareStreamEvent,
  CompareStreamEventHandler,
  CompareStreamOptions,
} from './classify'
export {
  cancelAccess,
  claimAccess,
  enterAccess,
  getAccessStatus,
  leaveAccess,
} from './access'
export type { AccessStateResponse, AccessWireState } from './access'
export {
  ACCESS_TICKET_HEADER,
  clearAccessTicket,
  getAccessTicket,
  readAccessTicket,
  saveAccessTicket,
  setAccessErrorHandler,
} from './accessTicket'
export { ERROR_MESSAGES, friendlyErrorMessage } from './errors'
export { arenaLeaderboard, arenaNext, arenaVote } from './arena'
export {
  FEEDBACK_READY,
  getFeedbackStats,
  isDuplicateFeedback,
  submitFeedback,
} from './feedback'
export type { FeedbackPayload, FeedbackStats, FeedbackSubmitResult } from './feedback'
export { DEMO_STATE_READY, getDemoState } from './demo'
export type { DemoState, DemoWorkerSource } from './demo'
export {
  BENCH_PK_READY,
  getBenchPk,
  normalizeBenchMetrics,
  normalizeBenchMeta,
  normalizeBenchPk,
} from './bench'
