# serving（阶段 2 / 3 / 4A + 前端对接）

本目录是统一服务层：阶段 2 提供只读网关与观测；阶段 3 通过 HTTP 调用现有 Flask worker 提供单模型分类与多模型对比；阶段 4A 为 LLM 上游调用增加总时间预算、有限重试与输出白名单校验。Vue 3 前端（`../frontend/`）只访问本网关，不直接访问 worker。

## 当前能力

- FastAPI 网关（Python 3.10）：
  - `GET /api/v1/healthz`：仅网关进程存活；
  - `GET /api/v1/readyz`：汇总 5 个 worker 的本地就绪状态（只读探测，整体只会是 `ok` / `degraded`）；
  - `GET /api/v1/models`：5 个模型的注册信息、启用状态、版本（null 表示不可得）与可用状态；
  - `GET /api/v1/stats`：单进程内存统计（重启清零；含按模型的 ok/error 计数）；
  - `POST /api/v1/classify`：单模型分类（`title` 必填，`model` 可选，默认 fasttext）；
  - `POST /api/v1/compare`：默认比较 5 个启用模型（含 llm），也可用 `models` 显式指定；合法请求统一 200，逐模型 ok/error。
- 阶段 4A LLM 韧性：上游总预算默认 12 s（`TMF_LLM_TOTAL_TIMEOUT_S`，须小于网关对 8105 的 15 s 超时）、关闭 SDK 自动重试、输出做 10 类白名单校验且至多重试一次；错误响应不包含供应商原文/标题/密钥。
- 5 个 worker 保留原 `POST /predict_api`，另有只读 `GET /healthz`、`/readyz`、`/metadata`。
- request ID、结构化日志（`logs/gateway.jsonl`）、基础统计。
- 前端：`../frontend/`（Vue 3 + TypeScript + Vite）已实现单模型分类、多模型对比、模型状态、LLM opt-in（默认不选）、错误与请求编号展示；契约见 [../docs/前端对接说明.md](../docs/前端对接说明.md)。
- 未实现：confidence/概率、批量分类、缓存、single-flight、级联、数据库/队列、**热搜**、鉴权、Docker、CI。

## 当前状态（截至 2026-10-20 晚）

| 项 | 状态 | 说明 / 证据 |
|---|---|---|
| 网关与 6 个 API | 已实现 | `serving/gateway/`；路由与行为以代码为准 |
| 6 个 API 的验收 | 已验证（历史） | 2026-10-20 记录 `verify_stage2.py` 与 `verify_stage3.py --llm-mode mock` 均 RESULT PASS、exit 0（见 [v2 档案](../docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md)）；**本次文档核对未重新运行** |
| 网关运行稳定性 | 已验证（历史） | 2026-10-20 17:10 启动 → 18:14 最后请求 → 18:17 停止；`logs/gateway.jsonl` 无 `level=ERROR`、无 500 |
| Vue 前端 | 已实现，接口层已验证 | `frontend/` + `frontend/dist/`（构建于 2026-10-20 17:19）；浏览器人工视觉验收未完成 |
| 真实 LLM provider | 部分验证（有成功调用，无系统评估） | 网关日志有 16 次 `model_id=llm`、`model_status=ok` 调用（2026-10-20 16:22–18:06，0.5–4.0 s），说明当时 provider/密钥可调用；验证脚本仍只用 mock；未做准确性/成本/稳定性评估 |
| 热搜 | 未实现 | 无代码；数据源选型未决 |

- 最近一次运行痕迹：2026-10-20 17:09–17:10 全量启动，18:17 `stage2.ps1 -Action stop` 输出"全部已停止，状态文件已清理"；**当前应处于未运行状态**（`serving/run/` 无 `stage2-state.json`）。
- 服务地址绑定 `192.168.12.54`（`serving/config/processes.yaml`、`workers.yaml` 与各 `api_flask_server.py`），换 IP/机器需同步修改。

## 启停

```powershell
pwsh -File serving/scripts/stage2.ps1 -Action status
pwsh -File serving/scripts/stage2.ps1 -Action start
pwsh -File serving/scripts/stage2.ps1 -Action stop
```

> 若 Windows PowerShell 执行策略阻止脚本，请使用：
> `powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action status`
> 脚本为 UTF-8（含 BOM），便于 Windows PowerShell 5.1 正确解析中文。

状态文件：`serving/run/stage2-state.json`；日志：`serving/logs/`。
脚本只停止由其启动且命令行归属匹配的进程，不会终止同端口未知进程。
配置可用 `serving/scripts/validate_config.py` 单独校验（不启动服务）。

> Windows 启动脚本使用 `py3109` 解释器运行网关，并从相邻的 `venv-gateway\Lib\site-packages` 加载网关依赖，以规避该机器上网关虚拟环境的 `_ssl` DLL 加载故障。worker 仍使用 `py3109`。网关对 worker 的 HTTP 调用显式设置 `trust_env=False`，避免系统代理拦截内网直连。

## 验收

先 `start`，再运行（命令与结果均属历史记录，最近一次执行见 [v2 档案](../docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md)）：

```powershell
E:\AIModels\test\TouManFen\venv-gateway\Scripts\python.exe serving/scripts/verify_stage2.py
E:\AIModels\test\TouManFen\venv-gateway\Scripts\python.exe serving/scripts/verify_stage3.py --llm-mode skip
```

`--llm-mode skip` 只验证 4 个本地模型与不涉及 LLM 的 compare；不会请求 8105。

LLM mock 验证（不访问真实 provider）：

1. 停止由启动脚本管理的真实 LLM worker（只停止该进程）；
2. 启动 mock：
   `venv-gateway\Scripts\python.exe serving\scripts\mock_worker.py --port 8105`
3. 运行：
   `venv-gateway\Scripts\python.exe serving/scripts/verify_stage3.py --llm-mode mock`
4. 停止 mock，再执行 `stage2.ps1 -Action start` 恢复真实 LLM worker。

## 边界

- **验证脚本**不调用真实 LLM provider，LLM 的验证测试只通过 mock worker；但运行时 8105 若是真实 worker，前端/调用方发起的 LLM 分类会真实调用外部 provider（可能计费）；
- 不抓取热搜、不训练模型、不修改 `py3109`；
- LLM 的 `/readyz` 仅表示本地服务与客户端配置已初始化，不代表供应商密钥有效；
- 前端只依赖本网关；网关不 import torch/transformers，也不加载模型权重。
