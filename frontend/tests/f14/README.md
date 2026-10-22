# F14 前端质量横切验收 harness（mock，不进入 `src/`）

本目录只服务于 **F14 焦点/键盘/aria-live/reduced-motion/性能横切验收**：
不修改后端、不访问真实网关 / worker / 真实 LLM、不依赖真实服务。
所有接口响应均由 `mock-server.mjs` 显式构造并标注 mock；测试用脚本不进入 `src/`。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | `?mode=app` 挂载真实 `App`（含 NavBar/ClassifyPanel/ResultStage/ArenaPage/BenchPage/Toaster）；`?mode=cards50` 挂载 50 张真实 `ResultCard`。向 `window.__f14` 暴露 active/焦点环/aria-live/输入延迟测量钩子。 |
| `mock-server.mjs` | 本地 mock API：models / classify / compare / feedback / arena / bench；`/__scenario?name=normal\|slow` 控制加载延迟；`/__log` 输出请求日志。 |
| `vite.harness.config.mjs` | 独立 Vite 构建：`envDir=tests/f14/env`，产物写 `tests/f14/dist`（运行结束自动清理，不碰 `frontend/dist`）。 |
| `run-scenarios.mjs` | 验收运行器：构建 → 启动 mock → Edge headless/CDP 真实键鼠事件 → 断言 → `artifacts/f14-report.json` + 截图；退出码 0/1。 |
| `artifacts/` | 每次运行覆盖写：报告 + 截图（skip link、焦点环、暗色、结果、反馈、Arena、reduced-motion、移动菜单、CPU、50 卡）。 |

## 运行

在 `frontend/` 目录下（Node 24 + Edge 155 已验证；端口 `18191` / `9336` 需空闲）：

```powershell
node tests/f14/run-scenarios.mjs
```

- 退出码 `0` = 全部断言 PASS；`1` = 失败或异常。
- 运行结束自动删除 `tests/f14/dist`、`tests/f14/env`，并关闭 mock/Edge。
- 仅调试某个场景：`$env:F14_ONLY='keyboard-only-flow,reduced-motion'` 后运行。
- 真实键盘事件通过 CDP `Input.dispatchKeyEvent` / `Input.insertText` 注入（非合成 DOM 事件）。

## 场景与断言重点

| 场景 | 断言重点 |
|---|---|
| S1 skip link / 焦点环 / Tab 顺序 | skip link 是首个可聚焦元素且未聚焦时移出视口；Enter 激活后焦点落到 `<main id="main-content" tabindex="-1">`；品牌 → 5 个导航链接的 Tab 顺序；亮/暗主题下焦点环 ≥2px 实线、未被 `overflow` 裁切、暗色对比度 ≥3:1 |
| S2 键盘-only 全流程 | 真实按键 Tab 到标题框并输入 → Tab 到提交并 Enter → 结果卡出现且只播报一条 `aria-live` 摘要（结果区不再整体 live）；反馈面板打开焦点进第一项、Tab 循环（首尾回绕）、Esc 关闭并归还焦点；键盘选中纠正类别并提交（「待审核」单条播报）；输出焦点回到触发按钮；键盘导航到 `#arena` 投票、翻牌后焦点落到揭晓状态区；「再抽一题」可继续 |
| S3 reduced-motion | `prefers-reduced-motion: reduce` 下：F6 分数条、结果区、loading spinner、F10 翻牌、Toaster、F11 条形动画/过渡均 ≤1ms；F11 指标切换由独立摘要 `aria-live` 播报 |
| S4 移动菜单 | 390×844 模拟视口：菜单按钮可见、导航默认收起；Enter 打开（`aria-expanded`/`aria-controls`）、Tab 进入第一项且焦点环可见；Esc 关闭并归还焦点 |
| S5 6× CPU throttle | CDP `Emulation.setCPUThrottlingRate({rate:6})`（模拟）：输入 → DOM 更新中位延迟 <250ms、字符计数同步、页面可滚动、按钮点击与真实键盘输入仍可用 |
| S6 50 张结果卡 | 挂载 50 张真实 ResultCard：滚动到第 50 张、反馈按钮可聚焦并用键盘打开面板（`useId` 唯一面板 id）、Esc 关闭、更新一张卡后仍 50 张且延迟 <250ms |

> 说明：`reduced-motion`、移动视口、6× CPU 均为 **CDP 模拟**，非真实低端设备；报告
> `f14-report.json` 的 `environment` 字段记录模拟参数。真实 B7/B8/B4 链路由各自验收脚本负责。
