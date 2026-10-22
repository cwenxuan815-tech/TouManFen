# F11 PK 榜单页验收 harness（mock，不进入 `src/`）

只服务于 **F11「PK 榜单页」前端验收**：不启动真实后端、不调用真实 LLM、不访问真实 worker。
`BenchPage.vue` 请求的 `GET /api/v1/bench/pk` 由本目录 `mock-server.mjs` 按场景返回。

> ⚠️ 当前 `artifacts/bench/pk_*.json` 尚未生成（B8 后端进行中），fixture
> `fixtures/bench_pk.mock.json` 是**结构一致的显式 mock**（部分数值取自 `docs/实测指标.md`）。
> B8 产物落地后，只需把 `fixtures/bench_pk.mock.json` 换成真实产物（或让 mock server 指向它），
> 页面代码无需改动（`api/bench.ts` 同时兼容 raw artifact 与 `{request_id, artifact_path, artifact}` 包装层）。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 挂载真实 `BenchPage`，向 `window.__f11` 暴露 DOM 快照与指标切换。 |
| `mock-server.mjs` | 本地 mock HTTP server：`/api/v1/bench/pk` 按 scenario 返回完整/包装层/空/500/仅路径；记录请求。 |
| `fixtures/bench_pk.mock.json` | 显式 mock fixture（5 模型 + 加权投票集成 + oracle，LLM status=missing）。 |
| `vite.harness.config.mjs` | 独立构建（envDir 指向本目录 env；产物 `tests/f11/dist` 被 gitignore 忽略，不碰 `frontend/dist`）。 |
| `run-scenarios.mjs` | 构建 harness → 启动 mock server → Edge headless/CDP 驱动并断言 → 输出 PASS/FAIL、退出码、证据与截图。 |
| `artifacts/` | `f11-report.json` + 各场景截图（每次运行覆盖）。 |

## 运行

在 `frontend/` 目录下：

```powershell
node tests/f11/run-scenarios.mjs
```

- 退出码 `0` = 全部断言 PASS；`1` = 有失败断言或运行异常。
- 需 `127.0.0.1:18090`（mock server）与 `9335`（Edge CDP）空闲。
- `tests/f11/env/.env.local` 由运行器自动写入。

## 场景

| scenario | 覆盖点 |
|---|---|
| `ready` | 完整产物：meta 逐项与 JSON 一致；7 行（5 模型 + 集成 + oracle）；F1/延迟/体积三指标切换数值与条形严格对应、无残留；LLM missing 显示「未纳入 · 外部 Provider 未授权」且不画条；oracle 参考线与「非独立模型」标注；禁用词「实时准确率/线上准确率」不出现。 |
| `wrapped` | `{request_id, artifact_path, artifact}` 包装层兼容。 |
| `empty` | 空产物 → AsyncState empty，不显示任何数值。 |
| `error` | 接口 500 → AsyncState error + 重试，不显示假数据。 |
| `path-only` | 只返回 `artifact_path` → 错误提示，绝不伪造指标。 |
