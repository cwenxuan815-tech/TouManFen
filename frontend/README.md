# TMF 前端

TMF 新闻标题分类验证页面，使用 Vue 3、TypeScript 和 Vite。当前包含单模型/多模型对比、SSE 流式、反馈、竞技场、PK 榜、主题/大屏与演示来源/离线快照；热搜功能仍暂缓。

> **最终状态（2026-10-22）**：已完成 F1–F16 中与本阶段相关的前端能力（分类/对比/SSE/反馈/竞技场/PK/主题/大屏/可访问性/F15 来源）；`vue-tsc`/`pnpm build` 通过；F14/F15/F10/F11/F9 浏览器 harness 有记录。F13/D4 设备为 Edge headless 模拟，**未做真实投影/手机人工视觉验收**。
>
> **运行方式**：本地开发默认 `127.0.0.1:5173`。从仓库根目录运行 `启动TMF.bat` 可自动探测电脑 LAN IPv4、启动后端与前端并等待前端就绪；手机访问脚本显示的 URL。停止时运行根目录 `停止TMF.bat`。真实手机视觉验收仍需在目标设备上完成。

## 环境

- Node.js 24 LTS（Vue 官方快速开始要求 `^22.18.0 || >=24.12.0`）+ pnpm
- 已启动的 TMF 后端（默认 `http://127.0.0.1:8000`）

## 启动

```powershell
cd frontend
pnpm install   # 仅首次
pnpm dev
```

浏览器打开 `http://127.0.0.1:5173`（默认）。前端监听地址/端口可用环境变量覆盖：

| 变量 | 作用 | 默认 |
|---|---|---|
| `TMF_FRONTEND_HOST` | Vite 监听地址 | `127.0.0.1` |
| `TMF_FRONTEND_PORT` | Vite 端口 | `5173` |
| `VITE_API_BASE_URL` | 前端访问网关的基址 | `http://127.0.0.1:8000` |

可在 shell 环境变量或 `frontend/.env.local`（不入库）中设置；示例见 `.env.example`。
**一键局域网启动（推荐）**：从仓库根目录运行 `启动TMF.bat`。它会发现当前默认路由的 Wi-Fi IPv4（无 Wi-Fi 时回退到有线网卡），设置 `TMF_FRONTEND_HOST=0.0.0.0`、`TMF_GATEWAY_HOST=0.0.0.0`、`TMF_BIND_HOST=127.0.0.1`、`TMF_WORKER_HOST=127.0.0.1`，并按当前 IP 配置 CORS 与 `VITE_API_BASE_URL`。前端和网关对手机开放，模型 worker 仅在电脑本机监听。

手动启动时，将 `<LAN_IP>` 换成电脑当前 Wi-Fi IPv4 地址：

```powershell
$env:TMF_FRONTEND_HOST='0.0.0.0'
$env:TMF_GATEWAY_HOST='0.0.0.0'
$env:TMF_BIND_HOST='127.0.0.1'
$env:TMF_WORKER_HOST='127.0.0.1'
$env:TMF_FRONTEND_PORT='5173'
$env:TMF_CORS_ORIGINS='http://<LAN_IP>:5173'
$env:VITE_API_BASE_URL='http://<LAN_IP>:8000'
powershell -NoProfile -ExecutionPolicy Bypass -File ..\serving\scripts\stage2.ps1 -Action start
pnpm dev
```

手机访问 `http://<LAN_IP>:5173`。Windows Defender 防火墙需在可信的“专用网络”放行 TCP **5173 和 8000**；启动脚本会提示此要求，但不会自行添加防火墙规则。worker 端口留在本机，不要向局域网开放。若仍无法连接，检查电脑与手机是否在同一可互访的子网、路由器是否开启 AP/客户端隔离，以及电脑当前 IPv4 地址是否变化。连接同一个 Wi-Fi 通常意味着在同一局域网，但访客 Wi-Fi、VLAN 或客户端隔离会阻止设备互访。

停止时运行仓库根目录 `停止TMF.bat`。后端脚本优先按状态文件停止进程；状态文件缺失时会按专属启动标记识别遗留 worker，并核对网关/Vite 命令行及监听端口。脚本最后检查相关端口并报告仍在监听的进程，不会仅凭端口号结束进程。

主导航根据 hash 路由同步当前项；进入 `#arena`、`#bench`、`#models`、`#about` 时会高亮对应导航，默认首页和 `#lab` 高亮分类实验台。

> 监听地址 ≠ 访问地址：`0.0.0.0` 只用于监听，浏览器不能用它连接。

前端不需要也不得配置 LLM 密钥。

## 已知约束（非安全上下文）

通过非 localhost 的 HTTP 地址（例如局域网 IP）访问时属于**非安全上下文**（只有 HTTPS 和 localhost 才是安全上下文），因此：

- `crypto.randomUUID()` 不可用。`src/api.ts` 已改为「`randomUUID` → `getRandomValues` 生成 UUID → 时间戳兜底」三级回退，请勿再直接调用 `crypto.randomUUID()`；
- 其他带 `[SecureContext]` 标记的浏览器 API（如 `navigator.clipboard`、`crypto.subtle`）同样不可用。

## LLM 安全提示

默认不把 LLM 纳入分类/对比请求。界面中的“大模型”开关需要用户主动开启；真实供应商未做系统评估（2026-10-20 有 16 次经网关的真实调用日志，但准确性/费用/稳定性未评估），调用可能产生费用。开发联调优先使用后端 LLM mock，真实调用需先确认后端 8105 当前运行的 worker。

## 页面设计

借鉴 vivo 官网简洁导航、宽阔留白、清晰大标题和蓝色科技氛围，但使用 TMF 自有品牌标识、颜色和 CSS 几何视觉，不复用 vivo 商标或页面素材。


## 最终收尾补充（2026-10-22）

- 页面：默认 `#lab`；`#arena`/`#bench` 懒挂载（默认首页不会触发竞技场 5 模型抽题）；导航含分类实验台/模型矩阵/竞技场/PK 榜/关于。
- 主题：`tokens.css` 双主题 + `useTheme`（系统默认、手动切换、localStorage）；大屏模式 `useLargeScreen`（×1.25、隐藏调试、全屏失败降级）；三档断点。
- 结果/反馈：F6 未校准分数/Top-3/拒识/threshold_meta/人工反馈参考；F8 pending 反馈；F10 匿名竞技场 + 翻牌 + Wilson 预热榜；F11 真实 `GET /bench/pk` 离线榜（LLM missing 显示未纳入）。
- 质量：F14 skip link、`:focus-visible`、反馈面板焦点循环/Escape、移动菜单 Escape、aria-live 单次播报、reduced-motion、50 卡与 6× CPU 模拟验收；F16 安全上下文复制降级。
- 数据真实性：页面启动即请求 `GET /api/v1/demo/state`（服务端 metadata 判定 mock/live；混合/缺失=unknown+来源明细）；结果 `data_source` 来自后端 worker metadata；不根据 URL/可达性猜 live；`public/snapshot.json` 为 cached 演示数据，页面级自动 cached 回退已接线（显示离线快照+生成时间，恢复后退出）。
- 限制：F13 设备验收为 Edge headless 模拟；无真实浏览器人工视觉验收；未调用真实 LLM/用户服务。详细命令见 D1 文档与 `frontend/tests/f9-f14`。
