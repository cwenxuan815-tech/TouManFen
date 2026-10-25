# F17 批量分类前端验收 harness（mock，不进入 `src/`）

本目录只服务于 **阶段 2B：单条/多条标题输入与批量分类前端验收**，不访问真实网关、worker 或 LLM。
所有 `/api/v1/*` 响应由 `mock-server.mjs` 显式构造并标注 mock。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 挂载真实 `ClassifyPanel` + `Toaster`，暴露输入、模式切换、提交、取消、重试和 DOM 快照钩子。 |
| `mock-server.mjs` | 本地 mock API：`/classify`、`/classify/batch`、`/feedback/stats`；`/__scenario` 切换正常/部分失败/分片失败/慢分片，`/__log` 读取请求与并发记录。 |
| `vite.harness.config.mjs` | 独立 Vite 构建，产物写入 `tests/f17/dist`（已由 `frontend/.gitignore` 的 `dist/` 忽略）。 |
| `run-scenarios.mjs` | 构建 harness、启动 mock server、启动 Edge headless/CDP、执行场景并断言。 |

## 运行

```powershell
cd frontend
node tests/f17/run-scenarios.mjs
```

退出码 `0` = 全部断言通过；`1` = 有失败或运行异常。运行器会覆盖进程环境中的
`VITE_API_BASE_URL`，避免继承开发 shell 的局域网 API 地址。

## 最近结果（2026-10-25）

- 2026-10-25 状态：阶段 2B 多条输入已交付，并随提交 `afd3ff2` 纳入版本库。
- 本 harness 结果以运行退出码为准（未记录固定断言数）。
- 仍为 mock/headless，不代表真实网关的批量链路已验收。
