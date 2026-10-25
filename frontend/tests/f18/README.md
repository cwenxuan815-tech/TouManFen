# F18 文件解析纯函数验收 harness（无 UI、无后端）

本目录只服务于 **阶段 3A：浏览器本地 TXT/CSV 解码与解析纯函数**。
不读取真实用户文件、不访问网关、不写数据库、不调用 LLM。

## 组成

| 文件 | 职责 |
|---|---|
| `harness.html` / `harness.ts` | 在 Edge/Chromium 中加载 `src/utils/titleFiles.ts`，构造内存字节并执行解析断言；同时检查浏览器 `TextDecoder('gb18030')`。 |
| `vite.harness.config.mjs` | 独立 Vite 构建，产物写入 `tests/f18/dist`（已被 `frontend/.gitignore` 的 `dist/` 忽略）。 |
| `run-scenarios.mjs` | 构建 harness、启动本地静态服务器、启动 Edge headless/CDP、读取断言结果。 |

## 运行

```powershell
cd frontend
node tests/f18/run-scenarios.mjs
```

退出码 `0` = 全部断言通过；`1` = 有失败或运行异常。
浏览器验证不能由 Node 测试替代；本 harness 的目标浏览器是 Edge/Chromium。
