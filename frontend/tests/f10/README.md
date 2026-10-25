# F10 竞技场 UI 验收 harness（mock，不进入 `src/`）

本目录只服务于 **F10 `/arena` 竞技场前端验收**，不修改后端、不访问真实网关 / worker / 真实 LLM、
不依赖真实服务。所有 `/api/v1/arena/*` 响应均由 `mock-server.mjs` 显式构造并标注 mock。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 挂载真实的 `ArenaPage`（内部含 `useArena` + `ArenaAnonymousCard` + `ArenaLeaderboard`）与 F4 `Toaster`；向 `window.__f10` 暴露 `dom / clickCard / clickNext / clientId` 等测试钩子。 |
| `mock-server.mjs` | 本地可控 mock B7 API（HTTP，Node 标准库）：`/arena/next`、`/arena/vote`、`/arena/leaderboard`；`POST /__scenario` 切场景，`GET /__log` 输出请求日志、anon_id→模型映射与票数。 |
| `vite.harness.config.mjs` | 独立 Vite 构建配置：`envDir=tests/f10/env`（避免读取 `frontend/.env.local` 的局域网地址），产物写入 `tests/f10/dist`（已 gitignore，不碰 `frontend/dist`）。 |
| `run-scenarios.mjs` | 验收运行器：构建 harness → 启动 mock server → 启动 Edge headless → 用 CDP 采集 `Network.*` 日志 → 断言 → 输出 PASS/FAIL、证据 JSON、截图与退出码。 |
| `artifacts/` | 每次运行覆盖写：`f10-report.json`（全部断言与证据）+ 各场景截图。 |

## 运行

在 `frontend/` 目录下（本机 Node 24 + Edge 155 已验证；端口 `18181` / `9335` 需空闲）：

```powershell
node tests/f10/run-scenarios.mjs
```

- 退出码 `0` = 全部断言 PASS；`1` = 有失败断言或运行异常。
- Edge user-data-dir 使用系统临时目录，运行结束清理。
- `tests/f10/env/.env.local` 由运行器自动写入（`VITE_API_BASE_URL` 指向 mock server）；运行器同时覆盖进程环境中的同名变量，避免继承开发 shell 的局域网 API 地址。

## 最近结果（2026-10-25）

- 2026-10-25 记录：**10/10 场景 PASS**（退出码 `0`）。
- 四模型改造后 S1 场景 **36/36** 断言通过，A–D 四列等宽断言通过。
- 结果以 `artifacts/f10-report.json` 与运行退出码为准；仍为 mock/headless，不代表真实网关/B7 链路已验收。

## 场景与断言重点

| 场景 | title（mock 控制） | 断言重点 |
|---|---|---|
| S1 正常（预热） | `normal` | 投票前卡片 A–D 无背面/无 `model_id`；候选区与整页 DOM 无真实模型名；投票前**只**请求 `/arena/next`（无 leaderboard）；`/arena/next` 响应体无 `model_id/display_name`；投票请求体仅 `client_id/item_id/anon_id`；翻牌揭晓正确且动画 `arena-flip`≈200ms；预热榜显示 votes/Elo/Wilson 区间、无 rank/名次列；固定说明「人类偏好投票强度，仅供演示参考」；无「准确率/客观排名」；换下一题不重复请求榜单 |
| S2 就绪 | `ready` | 票数 ≥ 50 才出现名次列，rank 1..4 按名次升序；投票前仍不请求榜单 |
| S3 部分候选 | `partial` | `partial=true` 显示部分结果提示；3 张卡片仍可正确揭晓 |
| S4 重复投票 | `duplicate` | 409 `ARENA_DUPLICATE_VOTE` → Toaster 中文提示，不伪造揭晓、不请求榜单 |
| S5 限速 | `rate-limited` | 429 `ARENA_RATE_LIMITED` → 「投票太快了，请稍后再试。」+ 每分钟 20 票说明 |
| S6 网络错误 | `network` | 连接被断开 → `NETWORK_ERROR` 中文提示，不伪造揭晓 |
| S7 空题库 | `empty` | 503 `ARENA_NO_ITEMS` → AsyncState 空状态 + 「重新抽取」，恢复后可投票 |
| S8 减少动效 | `normal` + `Emulation.setEmulatedMedia` reduce | 立即揭晓；`animation-name=none`、动画/过渡时长 ≤1ms |
| S9 client_id 刷新保持 | `normal` | `localStorage['tmf.arena.client_id']` 刷新后不变，再次投票沿用同一 client_id；刷新后投票前不请求榜单 |
| S10 题目标题可读性 | `long-title` | 1440/760/390 视口下标题文本与接口一致、字号/行高/对比度达标、连续字符可换行、3 行内、无横向滚动、不与操作按钮重叠；390 暗色对比度；1440 大屏不重复放大；候选卡数量/可点击状态不变 |

> 真实 B7 后端（`serving/gateway/routers/arena.py`、`serving/services/arena.py`）的字段与错误码与
> `mock-server.mjs` 一致；本 harness **不**启动真实网关，真实链路端到端由 B7 的验收脚本负责。
