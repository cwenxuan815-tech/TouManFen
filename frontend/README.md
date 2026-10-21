# TMF 前端

TMF 新闻标题分类验证页面，使用 Vue 3、TypeScript 和 Vite。当前仅包含单条分类与模型对比；热搜功能暂缓。

> **当前状态（2026-10-21）**：已实现单条分类、多模型对比、模型就绪状态与 LLM opt-in；`pnpm type-check` / `pnpm build` 通过，`dist/` 已重建；接口层与无头浏览器冒烟有记录，**未做真实浏览器人工视觉验收**。
>
> **当前运行状态（2026-10-21）**：以 LAN 模式运行于 `http://192.168.12.54:5173`，API 基址为 `http://192.168.12.54:8000`（由本地未入库的 `.env.local` 指定）。

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
**局域网示例**：`TMF_FRONTEND_HOST=0.0.0.0`（监听所有网卡），浏览器访问 `http://<LAN_IP>:5173`，
并让后端 `TMF_CORS_ORIGINS` 允许该来源、`VITE_API_BASE_URL=http://<LAN_IP>:8000`。

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
