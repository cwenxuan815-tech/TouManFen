# F15 cached / 启动来源最小验收

前提：`pnpm build` 已生成 `dist/`（包含 `dist/snapshot.json`）；不启动真实后端/LLM/用户服务。

## 1. cached 回退

```powershell
cd frontend
pnpm build
node tests/f15/run-f15-cached.mjs
```

脚本行为：
- 用 Node 静态服务器托管 `dist/`（127.0.0.1:18081）；
- 启动 Edge headless + CDP，拦截 `/api/v1/*` 并模拟 `ConnectionRefused`；
- 页面应读取 `public/snapshot.json`，显示 `离线快照`、生成时间、8 条 cached 标题，不显示 snapshot 错误；
- 截图写入 `tests/f15/artifacts/cached-fallback.png`。

## 2. 启动来源状态（本轮补齐）

后端接口：`GET /api/v1/demo/state`（只读 worker metadata，不触发推理；`mock-` 前缀=mock、非 mock=live、缺失/不可达=unknown；全 ready 且来源一致才返回 mock/live）。

```powershell
$env:PYTHONPATH='E:\AIModels\test\TouManFen\TouManFenClassifier;E:\AIModels\test\TouManFen\venv-gateway\Lib\site-packages'
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_f15_startup.py
```

浏览器首屏 mock/live/mixed/unknown、`?demo=cached` 与分类后结果卡来源证据：
- `artifacts/d4/f15-startup-source-report.json`
- `artifacts/d4/f15-startup-mock.png`、`f15-startup-live.png`、`f15-startup-mixed.png`、`f15-startup-unknown.png`、`f15-startup-url-hint.png`

## 3. 边界

- 以上均为**浏览器/网络模拟**，不是真实断网、真实设备或真实 Provider 验收。
- 不调用真实 LLM，不修改用户 stage2/网络/数据库。
- 完整 D4 证据见 `artifacts/d4/` 和 `docs/彩排记录_2026-10-22.md`。
