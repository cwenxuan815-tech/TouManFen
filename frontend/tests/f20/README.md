# F20 阶段 5C 访问名额 UI harness

本目录验证**真实 App + `useAccess` + AccessGatePanel/AccessActiveBadge + 本地 mock access/business API**。不连接真实网关、worker 或 LLM，不启动用户服务，不启用生产 feature flag。

## 覆盖

- 默认关闭：access API 404 `ACCESS_DISABLED`，旧业务页面正常加载，业务请求不带 ticket。
- `ready -> claim -> active`：自动 claim、ticket 保存、active 倒计时、业务请求自动带 `X-Access-Ticket`、leave 不冷却。
- ready 15s 过期/claim 失败后的恢复。
- queued position/ahead、约 2s 轮询、页面隐藏降频、cancel。
- queue_full 不密集自动重试，手动重试进入。
- cooling 倒计时并恢复 active。
- active 到期后业务内容隐藏，无新业务请求。
- `ACCESS_UNAVAILABLE` / 身份错误 fail-closed。
- 旧 ticket 清理并重新 enter；ticket 不进入 URL。
- `ACCESS_POLL_THROTTLED` 退避、不显示致命错误。
- 模拟同源标签 `storage` 失效恢复。
- 390px 无横向溢出、取消按钮可见。
- access 启用时 compare 不建立 EventSource SSE，使用受门禁的 POST fallback 并带 ticket。
- OCR/微博热搜外部导航：两个链接存在、可见，href 精确为 `http://192.168.12.82:8000/#image` 与 `http://192.168.12.54:8080/`（access 未 active 时被 overlay 遮挡，属已知限制）。

## 运行

在 `frontend/` 下：

```powershell
node tests/f20/run-scenarios.mjs
```

脚本自行构建 `tests/f20/dist`，启动 127.0.0.1 临时 mock 端口和 Edge headless；退出码 `0` 表示全部断言通过。

## 边界

- mock access 的 active 租约使用短剩余时间（2–4s）缩短测试，不改变后端 180s 契约；倒计时只消费服务端 `remaining_seconds`。
- ticket 日志只记录“是否携带”，不保存 raw ticket。
- 未验证真实手机/真实浏览器人工视觉、真实多标签竞态和真实后端网络；同源标签用 `StorageEvent` 模拟。
- SSE 仍受浏览器 `EventSource` 无自定义 header 限制；本阶段不绕过门禁。

## 最近结果（2026-10-25）

- 2026-10-25 记录：**58 checks PASS**（退出码 `0`，含 OCR/微博热搜导航断言）。
- 2026-10-25 状态：阶段 5C 访问名额 UI 已交付，并随提交 `afd3ff2` 纳入版本库。
- access overlay 遮挡导航是已知限制；未做真实 LAN/手机验收，上述为 mock/headless 结果。
