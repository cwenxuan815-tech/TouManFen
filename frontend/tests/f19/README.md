# F19 阶段 3B 文件导入 UI harness（mock）

本目录验证 **TXT/CSV 文件选择、浏览器本地读取、解析预览、表头/标题列选择、坏行跳过、替换确认和批量接入**。
使用真实 `ClassifyPanel`，文件由浏览器内 `File` 对象构造，批量 API 由本地 mock 服务提供；不连接真实后端或 LLM。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 挂载真实 `ClassifyPanel` + `Toaster`；暴露文件选择、预览操作、确认/取消和 DOM 快照钩子。 |
| `vite.harness.config.mjs` | 独立 Vite 构建，产物写入 `tests/f19/dist`（被 `.gitignore` 的 `dist/` 忽略）。 |
| `run-scenarios.mjs` | 启动 F17 的本地 mock API + 静态服务、Edge headless/CDP，执行 UI 断言。 |

## 运行

```powershell
cd frontend
node tests/f19/run-scenarios.mjs
```

退出码 `0` = 全部断言通过；`1` = 有失败或运行异常。
浏览器目标为 Edge/Chromium；不读取真实用户文件，不访问真实网关。
