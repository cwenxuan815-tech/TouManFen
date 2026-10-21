# TMF 前端

TMF 新闻标题分类验证页面，使用 Vue 3、TypeScript 和 Vite。当前仅包含单条分类与模型对比；热搜功能暂缓。

> **当前状态（2026-10-20）**：已实现单条分类、多模型对比、模型就绪状态与 LLM opt-in；`pnpm build` 已生成 `dist/`（2026-10-20 17:19）；接口层与无头浏览器冒烟有记录，**未做真实浏览器人工视觉验收**。

## 环境

- Node.js 24 LTS（Vue 官方快速开始要求 `^22.18.0 || >=24.12.0`）
- pnpm
- 已启动的 TMF 后端，地址默认为 `http://192.168.12.54:8000`（局域网内其他成员也走这个地址）

## 启动

```powershell
cd frontend
pnpm install
pnpm dev
```

浏览器打开 `http://192.168.12.54:5173`（同网段其他成员使用同一地址）。后端地址可以通过复制 `.env.example` 为 `.env.local` 并修改 `VITE_API_BASE_URL` 覆盖。前端不需要也不得配置 LLM 密钥。

## 已知约束（局域网 HTTP 访问）

局域网成员通过 `http://192.168.12.54:5173` 访问时属于**非安全上下文**（只有 HTTPS 和 localhost 才是安全上下文），因此：

- `crypto.randomUUID()` 不可用。`src/api.ts` 已改为「`randomUUID` → `getRandomValues` 生成 UUID → 时间戳兜底」三级回退，请勿再直接调用 `crypto.randomUUID()`；
- 其他带 `[SecureContext]` 标记的浏览器 API（如 `navigator.clipboard`、`crypto.subtle`）同样不可用。

## LLM 安全提示

默认不把 LLM 纳入分类/对比请求。界面中的“大模型”开关需要用户主动开启；真实供应商未做系统评估（2026-10-20 有 16 次经网关的真实调用日志，但准确性/费用/稳定性未评估），调用可能产生费用。开发联调优先使用后端 LLM mock，真实调用需先确认后端 8105 当前运行的 worker。

## 页面设计

借鉴 vivo 官网简洁导航、宽阔留白、清晰大标题和蓝色科技氛围，但使用 TMF 自有品牌标识、颜色和 CSS 几何视觉，不复用 vivo 商标或页面素材。
