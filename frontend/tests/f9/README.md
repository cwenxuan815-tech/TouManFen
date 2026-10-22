# F9 流式对比面板验收 harness（mock，不进入 `src/`）

本目录只服务于 **F9 `/compare/stream` 前端验收**，不修改后端、不访问真实 worker / 真实 LLM、
不依赖真实服务。所有 SSE 事件与整页数据均由 `mock-server.mjs` 显式构造并标注 mock。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 挂载真实的 `ResultStage`（内部含 `useCompareStream` + `CompareBoard`），并向 `window.__f9` 暴露 `start / switchMode / snapshot / dom / clickSort`。 |
| `mock-server.mjs` | 本地可控 mock SSE server（HTTP，Node 标准库）：按 `title` 选择场景；记录请求、SSE emit 顺序、客户端断开时间；同时托管 harness 构建产物与 mock `POST /compare`。 |
| `vite.harness.config.mjs` | 独立 Vite 构建配置：`envDir=tests/f9/env`（避免读取 `frontend/.env.local` 的局域网地址），产物写入 `tests/f9/dist`（已被 `.gitignore` 忽略，不碰 `frontend/dist`）。 |
| `run-scenarios.mjs` | 验收运行器：构建 harness → 启动 mock server → 启动 Edge headless → 用 CDP 驱动浏览器、采集 `Network.*` / `Network.eventSourceMessageReceived` 日志 → 断言 → 输出 PASS/FAIL 与退出码。 |
| `artifacts/` | 每次运行覆盖写：`f9-report.json`（全部断言与证据）+ 各场景截图。 |

## 运行

在 `frontend/` 目录下（本机 Node 24 + Edge 155 已验证）：

```powershell
node tests/f9/run-scenarios.mjs
```

- 退出码 `0` = 全部断言 PASS；`1` = 有失败断言或运行异常。
- 需要 `127.0.0.1:18080`（mock server）与 `9334`（Edge CDP）空闲；Edge user-data-dir 使用系统临时目录，运行结束清理。
- `tests/f9/env/.env.local` 由运行器自动写入，用于把 harness 的 `VITE_API_BASE_URL` 指向 mock server。

## 场景（`title` 即 mock 控制通道，前端只按真实契约发 title/models）

| title | 场景 | 断言重点 |
|---|---|---|
| `f9-normal` | 正常多模型按序返回（心跳注释 + 无效 JSON + 重复 model） | 1 条 SSE GET、Content-Type `text/event-stream`、事件顺序、按 model_id 去重、无效 JSON 丢弃、首事件 < 1s、DOM 逐项落位/排序/分歧高亮 |
| `f9-llm-offline` | LLM 单模型 `status=error`，其余成功 | summary `partial` 5/4/1、LLM 卡片显示错误、其他成功项不被覆盖、不整页回退 |
| `f9-all-failed` | 全部模型 error | summary/done `all_failed`、概览「模型暂时无法完成判断」、无分歧徽标 |
| `f9-switch` | 模型慢速返回，中途切到单模型模式 | 连接在切换后 1.5s 内关闭、无后续更新、无重连、无 POST |
| `f9-fallback` | SSE 只发心跳、永不发事件 | 3s 无有效首事件 → 关闭 SSE → 仅 1 次 `POST /compare`、`fallback=true`、SSE 与 POST 结果不混合 |
| `f9-stream-error`（附加） | 先 1 个 model，再流级 `event: error` | 面板错误态 AsyncState、保留已到达结果、不触发 POST 回退 |
| `f9-connect-error`（附加） | SSE 端点 503 | 立即回退（<3s）、1 次 SSE GET + 1 次 POST、回退结果完整 |

> 真实 B6 后端（`serving/gateway/routers/stream.py`）的事件名/字段与 `mock-server.mjs` 一致；
> 本 harness **不**启动真实网关，真实链路端到端由 B6 的验收脚本负责。
