/**
 * F3：错误码 → 中文提示的兼容出口。
 *
 * 映射与 `friendlyErrorMessage()` 的实现集中在 `api/client.ts`（统一 API 客户端），
 * 本文件仅保留旧导入路径的 re-export，避免循环依赖。
 */
export { ERROR_MESSAGES, friendlyErrorMessage } from './client'
