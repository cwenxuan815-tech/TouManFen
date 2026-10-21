# serving（统一网关 / 模型适配层 / worker 契约）

本目录是统一服务层：FastAPI 网关负责模型注册、请求编排、超时/错误归一化、统计与观测；
`adapters/` 是网关内部的模型适配层；5 个 Flask worker 仍独立进程运行，通过统一
`PredictService` 契约提供预测。Vue 3 前端（`../frontend/`）只访问本网关。

> **当前运行状态（2026-10-21）**：LAN 模式，网关 `192.168.12.54:8000`，worker `8101/8103/8104/8105/8106`；`/api/v1/readyz = ok（5/5 up）`；前端 `192.168.12.54:5173`。默认值仍为 `127.0.0.1`，LAN 由环境变量覆盖。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `gateway/` | FastAPI 应用、路由、schema、错误、探测、统计、配置加载（composition root 在 `main.py` 的 lifespan） |
| `adapters/protocol.py` | `ModelAdapter` 协议、`AdapterResult` / `PredictionResult` 数据类 |
| `adapters/http_worker.py` | `HttpWorkerAdapter`：HTTP 调用单个 Flask worker，超时/非 200/非法 JSON/缺字段/非法类别归一化 |
| `adapters/llm.py` | `LlmAdapter`：LLM worker 适配器（仍是本地 HTTP；Provider 密钥只在 `04-LLM` 进程） |
| `models/` | `ModelRegistry`（纯元数据）、`http_adapter.py`（旧名兼容层，已弃用） |
| `services/classification.py` | 业务编排，只依赖 `Mapping[str, ModelAdapter]` |
| `config/` | `workers.yaml` / `models.yaml` / `processes.yaml`（默认值均为本地 `127.0.0.1`） |
| `scripts/` | 启停、配置校验、mock worker、验收脚本 |

装配规则：具体适配器只在 `gateway/main.py` 的 lifespan 实例化；业务层不 import 具体适配器类；
适配器不加载模型/权重、不读 `.env`、不访问外部 Provider。

## 当前能力

- `GET /api/v1/healthz`：仅网关进程存活；
- `GET /api/v1/readyz`：汇总 5 个 worker 的就绪状态（只读探测，整体为 `ok` / `degraded`）；
- `GET /api/v1/models`：5 个模型注册信息、启用状态、版本、可用状态；
- `GET /api/v1/stats`：单进程内存统计（重启清零，含按模型 ok/error 计数）；
- `POST /api/v1/classify`：单模型分类（`title` 必填，`model` 可选，默认 fasttext）；
- `POST /api/v1/compare`：默认比较 5 个启用模型（含 llm），也可用 `models` 显式指定；合法请求统一 200，逐模型 ok/error；
- 阶段 4A LLM 韧性：上游总预算默认 12 s（`TMF_LLM_TOTAL_TIMEOUT_S`，须小于网关对 8105 的 15 s 超时）、关闭 SDK 自动重试、输出 10 类白名单校验且至多重试一次；
- request ID、结构化日志（`logs/gateway.jsonl`）、基础统计；
- 未实现：confidence/概率、批量分类、缓存、single-flight、级联、数据库/队列、**热搜**、鉴权、Docker、CI。

## 配置（网络）

默认全部为 `127.0.0.1`，局域网通过环境变量覆盖（完整表见根 [README](../README.md) 5.5）：

| 变量 | 作用 |
|---|---|
| `TMF_BIND_HOST` | 5 个 Flask worker 监听地址 |
| `TMF_GATEWAY_HOST` / `TMF_GATEWAY_PORT` | 网关监听地址 / 端口 |
| `TMF_WORKER_HOST` | 网关调用 worker 的主机（端口沿用 `workers.yaml`） |
| `TMF_WORKER_URL_<ID>` | 单个 worker 完整 URL 覆盖（如 `TMF_WORKER_URL_RF`） |
| `TMF_FRONTEND_HOST` / `TMF_FRONTEND_PORT` | 默认 CORS 来源推导（`http://<host>:<port>`） |
| `TMF_CORS_ORIGINS` | 追加 CORS 来源（逗号分隔，禁止 `*`） |

`serving/scripts/validate_config.py` 会校验 YAML、端口、URL、host/port/CORS；非法变量直接失败并给出变量名。

## 启停

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action stop
```

- 状态文件：`serving/run/stage2-state.json`；日志：`serving/logs/`。
- 脚本只停止由其启动且命令行归属匹配的进程；`TMF_GATEWAY_HOST`/`TMF_GATEWAY_PORT`/`TMF_BIND_HOST` 会被脚本与子进程共同采用。
- 脚本为 UTF-8（含 BOM）+ CRLF，便于 Windows PowerShell 5.1 正确解析中文。
- 网关用 `py3109` 启动并从 `venv-gateway\Lib\site-packages` 注入依赖（规避该机 `_ssl` DLL 故障）；worker 仍用 `py3109`。

## 验收

配置/适配层/worker 契约（不加载模型、不启动服务）：

```powershell
$env:PYTHONPATH='E:\AIModels\test\TouManFen\venv-gateway\Lib\site-packages'
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_refactor_gateway.py
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_refactor_workers.py
```

端到端（先 start；真实 worker）：

```powershell
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_stage2.py
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_stage3.py --llm-mode skip
```

2026-10-21 重构后实测：上述四个脚本全部 `RESULT PASS`；`verify_stage3.py --llm-mode mock` 在 5 个 mock worker 栈上同样 PASS（不访问真实 provider）。

LLM mock 验证（不访问真实 Provider）：

1. 停止由启动脚本管理的真实 LLM worker（只停止该进程）；
2. 启动 LLM mock（`--special-titles` 启用 MOCK_* 错误/超时用例）：
   `D:\Miniconda3\envs\py3109\python.exe serving\scripts\mock_worker.py --port 8105 --model-id llm --special-titles`
3. 运行：`D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_stage3.py --llm-mode mock`
4. 停止 mock，再执行 `stage2.ps1 -Action start` 恢复真实 LLM worker。

> 其他模型也支持通用 mock（用于无模型环境）：
> `python serving/scripts/mock_worker.py --port 8101 --model-id rf --display-name mock-rf`

## 边界

- 验证脚本不调用真实 LLM provider；运行时 8105 若是真实 worker，前端/调用方发起的 LLM 分类会真实调用外部 provider（可能计费）；
- 网关不 import torch/transformers、不加载模型权重、不读取密钥；
- 不抓取热搜、不训练模型、不修改 `py3109`；
- LLM 的 `/readyz` 仅表示本地服务与客户端已初始化，不代表供应商密钥有效；
- 前端只依赖本网关；前端默认不把 LLM 纳入请求（opt-in）。
