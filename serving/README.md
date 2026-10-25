# serving（统一网关 / 模型适配层 / worker 契约）

本目录是统一服务层：FastAPI 网关负责模型注册、请求编排、超时/错误归一化、统计与观测；
`adapters/` 是网关内部的模型适配层；5 个 Flask worker 仍独立进程运行，通过统一
`PredictService` 契约提供预测。Vue 3 前端（`../frontend/`）只访问本网关。

> **最终状态（2026-10-25）**：网关/worker/适配层、单条/批量/对比/SSE/反馈/竞技场/bench/demo state、LLM 配额、访问名额控制（默认关闭）均已实现并提交 `afd3ff2`；前端多条输入与文件导入已交付。默认值仍为 `127.0.0.1`，LAN 由环境变量覆盖。**B8 LLM 仍 missing；真实 LAN/手机/真实 LLM 未验收；本轮未启动/压测用户服务**。

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
- `POST /api/v1/classify/batch`：阶段 2A 多标题 × 单模型批量分类；1–100 条、每条 ≤200 code point、总计 ≤20,000 code point，批量为 30s 总时限；v1 在 worker 调用前拒绝 `llm`；逐条结果保留顺序，支持 partial/all_failed；不返回 `human_feedback_hint`；前端多条输入（F17）与 TXT/CSV 文件导入（F18/F19）已交付；
- `POST /api/v1/compare`：默认比较 4 个本地模型（**B9 起 LLM 为 opt-in**，默认不含 llm），可用 `models` 显式指定；合法请求统一 200，逐模型 ok/error；
- 阶段 4A LLM 韧性：上游总预算默认 12 s（`TMF_LLM_TOTAL_TIMEOUT_S`，须小于网关对 8105 的 15 s 超时）、关闭 SDK 自动重试、输出 10 类白名单校验且至多重试一次；
- request ID、结构化日志（`logs/gateway.jsonl`）、基础统计；
- 已交付：未校准 `scores/top_k`、B3 软拒识、B4 反馈、B6 SSE、B7 竞技场、B8 bench、B11 data_source、批量后端 + 前端多条/文件导入、4A/4B/4C LLM 配额、5A–5D 访问控制（默认关闭）；仍未实现 calibrated confidence、缓存、single-flight、级联、热搜采集（仅保留外部导航链接）、鉴权、生产队列、Docker/CI、多标题 × 多模型。

## 配置（网络）

后端单机默认全部为 `127.0.0.1`。Windows 根目录 `启动TMF.bat` 会为多网卡/手机局域网模式自动配置：网关绑定 `0.0.0.0`，worker 仍绑定 `127.0.0.1`，网关也只通过 loopback 调用 worker；为检测到的各前端地址添加 CORS 来源。跨网段设备需能路由到运行电脑对应地址；停止使用根目录 `停止TMF.bat`。手动设置项如下（完整操作见根 [README](../README.md)）：

| 变量 | 作用 |
|---|---|
| `TMF_BIND_HOST` | 5 个 Flask worker 监听地址 |
| `TMF_GATEWAY_HOST` / `TMF_GATEWAY_PORT` | 网关监听地址 / 端口 |
| `TMF_WORKER_HOST` | 网关调用 worker 的主机（端口沿用 `workers.yaml`） |
| `TMF_WORKER_URL_<ID>` | 单个 worker 完整 URL 覆盖（如 `TMF_WORKER_URL_RF`） |
| `TMF_FRONTEND_HOST` / `TMF_FRONTEND_PORT` | 默认 CORS 来源推导（`http://<host>:<port>`） |
| `TMF_CORS_ORIGINS` | 追加 CORS 来源（逗号分隔，禁止 `*`） |
| `TMF_LAN_IPS` | Windows 启动脚本手动指定多个本机 IPv4（逗号分隔） |

手机访问时只需在可信专用网络放行 TCP 8000（网关）和 5173（前端）；worker 的 8101/8103/8104/8105/8106 端口不要暴露到局域网。启动脚本不会自行创建 Windows 防火墙规则。

`serving/scripts/validate_config.py` 会校验 YAML、端口、URL、host/port/CORS；非法变量直接失败并给出变量名。

## 启停

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File serving/scripts/stage2.ps1 -Action stop
```

- 状态文件：`serving/run/stage2-state.json`；日志：`serving/logs/`。
- 停止时优先按状态文件和命令行归属处理；状态记录缺失/遗漏时，会按 worker 专属 `--tmf-managed=stage2-...` 标记回收 worker，并要求网关/Vite 同时符合 TMF 命令行特征和对应监听端口。最后由 `check_tmf_ports.ps1` 核验端口；不要仅凭端口号手动结束进程。`TMF_GATEWAY_HOST`/`TMF_GATEWAY_PORT`/`TMF_BIND_HOST` 会被脚本与子进程共同采用。
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

LLM mock 验证（不访问真实 Provider，**不操作真实 LLM worker/用户 stage2**）：

```powershell
# 使用脚本自带的隔离栈：临时端口启动 5 个 mock worker + 临时网关，结束时释放端口/临时 DB
$env:PYTHONPATH='E:\AIModels\test\TouManFen\venv-gateway\Lib\site-packages'
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_stage3.py --llm-mode mock --isolate-mock
```

> 不要为了跑 mock 验证去停止用户正在运行的真实 LLM worker（8105）；`--isolate-mock` 与用户服务完全隔离。
> 其他模型也支持通用 mock（用于无模型环境），必须使用临时端口：
> `python serving/scripts/mock_worker.py --port <临时端口> --model-id rf --display-name mock-rf`


## LLM 配额基础设施（阶段 4A）

阶段 4A 当时只提供可单测的 IP 解析与 SQLite 配额存储，**尚未接入分类/compare/stream/arena 路由**；阶段 4B 已接入，见下一节。

- `serving/gateway/client_ip.py`：默认只信任 socket peer；仅 `TMF_TRUSTED_PROXY_CIDRS` 中显式配置的代理可提供 `X-Forwarded-For`/`Forwarded`。默认列表为空，`/0` 被拒绝；IPv4-mapped IPv6 归一为 IPv4。
- `serving/gateway/quota_config.py`：读取 `TMF_LLM_QUOTA_HMAC_KEY`（必需、至少 32 字节）与 `TMF_DB_PATH`（默认 `serving/run/tmf.db`）；key 缺失/过短时 fail-closed。key 轮换会改变 HMAC 索引，旧封禁记录不会自动迁移。
- `serving/services/llm_quota.py`：SQLite 表 `llm_ip_quota` / `llm_quota_event`；按 HMAC-SHA256(IP) 原子 `BEGIN IMMEDIATE` 预留，1–3 次允许、第 4 次拒绝；超时/Provider 失败不退款；数据库异常抛 `LlmQuotaStoreError`（不得 fail-open）。审计表预留 `operation`、`used_before/after`、`admin_note` 等字段，但本阶段不提供管理员命令/API。
- 隔离验收：`serving/scripts/verify_llm_quota_infra.py`（临时 DB/进程，不启动服务、不调用 Provider）。

## LLM 配额接入（阶段 4B）

4A 的存储已接入网关 LLM adapter 调用路径；每次真实 `LlmAdapter.predict` 前由 `QuotaedLlmAdapter` 原子 reserve 一次：

- 覆盖 `POST /api/v1/classify`、`POST /api/v1/compare`、`GET /api/v1/compare/stream`、`GET /api/v1/arena/next` 及其经过网关的 LLM adapter 路径；批量 `/classify/batch` 仍拒绝 LLM 且不扣额度。
- 第 1–3 次允许；第 4 次 HTTP **403 `LLM_QUOTA_EXHAUSTED`**（详情含 `limit=3`、`used=3`；不返回 IP/key）。store 配置/初始化/读写失败时 HTTP **503 `LLM_QUOTA_UNAVAILABLE`**，不调用 LLM adapter；无有效客户端身份时 403 `LLM_QUOTA_IDENTITY_INVALID`。
- `/compare`、`/compare/stream` 在 fanout 前做只读预检：已耗尽/store 不可用时整请求 403/503，不启动任何本地模型；真正 LLM 调用仍逐次原子 reserve，并发抢最后额度由 SQLite 事务拒绝，已启动的其他本地模型正常收尾。
- 竞技场默认只使用 4 个本地模型，刷新不触发 LLM、不消耗配额；匿名投票/Elo 不变。
- 启动时在 lifespan 初始化/关闭 store；缺少 `TMF_LLM_QUOTA_HMAC_KEY` 或 store 初始化失败时，非 LLM 功能照常，LLM 统一 503，不会静默放开。
- key 运行配置：`TMF_LLM_QUOTA_HMAC_KEY`，至少 32 字节，由操作员用密码管理器/安全随机源生成并注入进程环境；不要写仓库或 `.env`。key 轮换会改变全部 HMAC IP 索引，旧计数/封禁记录不会自动迁移，需离线管理员方案另行处理。
- 隔离验收：`serving/scripts/verify_llm_quota_gateway.py`（临时 SQLite + fake adapter + TestClient，不启动真实服务/Provider）。

### Uvicorn 启动身份边界（阶段 4B 收口）

`serving/scripts/stage2.ps1` 的 Start-Gateway 参数已加入 `--no-proxy-headers`：

```text
-m uvicorn serving.gateway.main:app
--host <host> --port <port>
--no-access-log
--no-proxy-headers
--log-level info
```

Uvicorn 0.54.0 支持该参数。关闭 Uvicorn 自身的 `X-Forwarded-For`/`X-Forwarded-Proto` 改写后，`request.client` 始终是真实 socket peer；`serving/gateway/client_ip.py` 成为唯一转发头信任边界，且只在显式 `TMF_TRUSTED_PROXY_CIDRS` 匹配时解析 `X-Forwarded-For`/`Forwarded`。未设置 `--forwarded-allow-ips`，避免与 `--no-proxy-headers` 冲突。

临时 Uvicorn 验证：`serving/scripts/verify_stage2_proxy_boundary.py`（临时 loopback 端口，不占用 8000/8101-8106，不启动用户服务）。结果覆盖：无代理时伪造 XFF/Forwarded 不改变身份；显式 `127.0.0.1/32` 时按可信链取右侧第一个非可信地址；全可信链 fail-closed。

网关配额不保护管理员/本机脚本直接调用 8105；LLM worker 仍仅 loopback 监听，这是既有边界。

### 离线管理员 CLI（阶段 4C）

只提供本机离线命令；没有 HTTP 管理 API、Web UI 或网络服务：

```powershell
# TMF_LLM_QUOTA_HMAC_KEY 必须由操作员注入当前终端环境；不要把 key 放进命令参数/仓库。
python serving/scripts/manage_llm_quota.py --db <quota.db> status <ip>
python serving/scripts/manage_llm_quota.py --db <quota.db> unblock <ip> `
  --reason "客服确认误触发，人工复核" --confirm RESET-LLM-QUOTA
```

- `status`：复用 `client_ip.py` 规范化 IP 与既有 HMAC；只显示该 IP 的 `used/limit`、`blocked`、首次/最近/封禁时间。找不到时显示“该 IP 尚无配额记录”；不列出整个 DB，不输出明文 IP。
- `unblock`：要求精确 `--confirm RESET-LLM-QUOTA` 且 `--reason` 非空；在一个 `BEGIN IMMEDIATE` 事务中把该行 `used=0`、`blocked_at=NULL`，并写入 `llm_quota_event`：`operation=admin_unblock`、`used_before/used_after`、`created_at`、`admin_note`、`request_id=offline-cli`、`route=offline-cli`。历史 reserve 事件保留；失败回滚；解除后该 IP 可重新使用 3 次，第 4 次仍拒绝。
- 没有 `clear`、`reset-all`、批量解除或模糊匹配；不删除任何配额/审计行。
- `--db` 省略时使用 `TMF_DB_PATH`，再回退 `serving/run/tmf.db`。正式库路径必须显式传 `--db` **并**加 `--allow-production-db`；否则拒绝且不打开数据库。临时测试库无需该确认参数。
- HMAC key 只从 `TMF_LLM_QUOTA_HMAC_KEY` 读取；缺失/过短返回非零且不输出 key。合法但错误的 key 无法与“新 IP”区分，会表现为无记录；轮换 key 后旧记录不会自动匹配或迁移。
- 退出码：`0` 成功；`1` DB/IP/记录/runtime 失败；`2` 参数、key 配置、确认短语或正式 DB 保护拒绝。
- 隔离验收：`serving/scripts/verify_llm_quota_cli.py`（临时 SQLite、运行时随机 key；不启动网关/worker、不调用 Provider、不触碰正式 DB）。

## 访问管理核心状态机（阶段 5A）

- 新增 `serving/services/access.py`：单进程内存 `AccessManager`，实现 active/ready/queued/cooling/expired/cancelled/queue_full 状态、FIFO、10 个 active/ready 上限、50 个 queued 上限、180/120/60/15 秒规则所需的内部 TTL 与 sweep；队列满时 `enter()` 返回 `queue_full`。
- 新增 `serving/scripts/verify_access_state.py`：fake clock/fake ticket generator 的状态机隔离验收。
- 进程重启后内存状态自然清空，旧 ticket 失效。
- 该 manager 只接收调用方已脱敏的 `ip_key`，不负责 IP 解析/HMAC。

## 访问管理 API 与业务门禁（阶段 5B）

### Feature flag

`TMF_ACCESS_CONTROL_ENABLED` 默认 **false**：

- false：不创建 manager，不要求 ticket，现有 API 行为不变；access API 返回 404 `ACCESS_DISABLED`。
- true：网关启动时用 `TMF_LLM_QUOTA_HMAC_KEY`（与 LLM quota 共用，域分离）初始化内存 manager，并对业务 API 全面门禁。缺少该 key 或初始化失败时启动失败，**不会自动降级为 false**。
- 启用需要显式设置环境变量并重启网关；本阶段没有替用户打开开关。
- **变量来源**：`stage2.ps1` 和根目录 `启动TMF.bat` 不会从 `.env` 加载访问控制配置（`TMF_ACCESS_CONTROL_ENABLED` 和 `TMF_LLM_QUOTA_HMAC_KEY` 必须来自启动进程环境）；必须先在启动 `启动TMF.bat` 的同一 PowerShell/CMD 环境设置，或由操作员在适当的用户环境配置后再启动。双击 `.bat` 不会继承另一个终端会话中的临时变量。注意：`stage2.ps1` 会读取 `04-LLM/.env` 中的 `DEEPSEEK_API_KEY`，仅用于判断是否启动 LLM worker，不会把 access 配置加载到网关。
- **启用前**：确认 key 至少 32 字节（只检查有效/无效，不打印 key）、开关为 true、Uvicorn 单进程、CORS 覆盖实际前端 origin、防火墙放行 5173/8000、worker 端口保持 loopback。完整命令和检查清单见根 [README「访问名额控制」](../README.md#31-访问名额控制可选默认关闭)。
- **回滚**：在同一环境把 `TMF_ACCESS_CONTROL_ENABLED` 设为 false 后重启网关；旧 ticket/内存队列/冷却随网关进程重建而失效，前端刷新后会检测到 `ACCESS_DISABLED`。不要删除 quota SQLite，也不要随意轮换共用 key。

### Access API

ticket 只通过 `X-Access-Ticket` 请求头传递，不进入 URL/query/日志。

```text
POST /api/v1/access/enter     (ticket 可选；刷新恢复时也可不带)
GET  /api/v1/access/status    (需要 ticket)
POST /api/v1/access/claim     (需要 ticket；ready 15s 内)
POST /api/v1/access/leave     (需要 ticket；提前离开不冷却)
POST /api/v1/access/cancel    (需要 ticket；取消 queued)
```

- `enter` 返回 `active/ready/queued/cooling` 及 ticket、position/ahead、remaining_seconds；队列满返回 HTTP 429 `ACCESS_QUEUE_FULL`。
- `status` 返回当前状态但**不返回 ticket**，且不续期；阶段 5C 前端已有排队/冷却轮询客户端，目标约 2s + jitter，页面隐藏时约 8s，恢复可见立即刷新。
- 状态轮询有每 IP 最小间隔保护（`TMF_ACCESS_STATUS_MIN_INTERVAL_MS`，默认 1000ms），超频返回 429 `ACCESS_POLL_THROTTLED`。
- ticket 缺失/伪造/归属错误返回 403 `ACCESS_TICKET_INVALID`；不透露其他 IP 状态。
- manager 不可用返回 503 `ACCESS_UNAVAILABLE`；不会 fail-open。

### 业务门禁

启用后，除以下路径外，所有业务 API 必须携带当前 IP 的 active ticket：

- `/api/v1/access/*`
- `/api/v1/healthz`、`/api/v1/readyz`
- CORS `OPTIONS`

覆盖 models/stats/demo/bench、classify/batch/compare/SSE、arena、feedback 及所有未来新增业务路由。无 ticket -> 403 `ACCESS_REQUIRED`；queued -> 429 `ACCESS_QUEUED`；ready -> 409 `ACCESS_CLAIM_REQUIRED`；cooling -> 429 `ACCESS_COOLING`；过期 -> 403 `ACCESS_TICKET_INVALID`。

后台仍会在 `StreamingResponse` 前校验 ticket，并可用 active 剩余租约限制流生命周期（到期发送 `event: error` + `ACCESS_EXPIRED`）。但阶段 5C 前端在 access 启用时**不建立 EventSource**：浏览器 `EventSource` 不能添加 `X-Access-Ticket`，compare 走受同一门禁保护的 `POST /compare` fallback；access 关闭时 SSE 原行为不变。

访问管理是 IP 名额限制，不是用户认证；NAT 共享名额。v1 必须保持单 Uvicorn worker（stage2 当前未设置 `--workers`），重启网关会清空内存中的 active/queued/cooling 并使旧 ticket 失效。隔离验收：`serving/scripts/verify_access_gateway.py`（临时 app/内存 manager/fake classifier，不启动服务、不调用真实 LLM）。

## 边界

- 验证脚本不调用真实 LLM provider；运行时 8105 若是真实 worker，前端/调用方发起的 LLM 分类会真实调用外部 provider（可能计费）；
- 网关不 import torch/transformers、不加载模型权重、不读取密钥；
- 不抓取热搜、不训练模型、不修改 `py3109`；
- LLM 的 `/readyz` 仅表示本地服务与客户端已初始化，不代表供应商密钥有效；
- 前端只依赖本网关；前端默认不把 LLM 纳入请求（opt-in）。


## 最终收尾补充（B9/B11/B10，2026-10-22）

- **LLM opt-in**：`llm.compare_by_default=false`；默认 compare 不含 LLM；显式 `models` 仍可选；mock 离线时为 `partial`，其余模型结果保留。
- **data_source**：`HttpWorkerAdapter`/`LlmAdapter` 依据 `/metadata.display_name`（`mock-` 前缀=mock，否则 live；无法判定=unknown，绝不猜 live）；透传 classify/compare 每项/SSE model；统计只计 live。
- **F15 启动来源**：`GET /api/v1/demo/state` 只读探测 worker metadata，返回 mock/live/unknown、ready/total、worker_sources；不触发模型推理、不推断 live。
- **B10 回归**：`verify_refactor_gateway.py` 101 PASS、`verify_refactor_workers.py` 93 PASS、`verify_stage3.py --llm-mode mock --isolate-mock` 59 PASS；均使用隔离 mock 端口/临时 DB，不启动用户 stage2、不调用真实 LLM。
- **接口变更/口径**：见 `docs/接口变更记录_2026-10-21.md`、`docs/指标来源与三类数据.md`、`docs/反馈数据边界与文案.md`、`docs/演示三态.md`、`docs/并发与超时契约.md`。
- **最终状态（2026-10-22）**：B8 LLM 仍 missing（未授权真实 Provider）；F15 启动来源 + 页面级 cached 自动回退已接线并验证（`/api/v1/demo/state`，后端不可达时读 `public/snapshot.json`）；F13/D4 设备为浏览器模拟；D4 已用隔离 mock 完成 10 步与 5 故障场景，不做真实 Provider/真实设备验收。

## 2026-10-25 状态回填

- **提交**：访问控制、批量/文件导入、LLM 配额与 F17–F20 测试已随 `afd3ff2 feat: complete access control and delivery integration`（2026-10-25 11:47 +0800）入库，`main` 与 `origin/main` 同步。
- **本轮隔离复跑**（未启动真实网关/worker、未调用真实 Provider）：
  - `verify_batch.py`：`RESULT PASS`（TestClient + fake adapter）；
  - `verify_access_state.py`：**90 checks PASS**；
  - `verify_access_gateway.py`：**80 checks PASS**；
  - `verify_llm_quota_infra.py`：`RESULT PASS`；
  - `verify_llm_quota_gateway.py`：**59 checks PASS**（需先清除进程环境变量 `TMF_ACCESS_CONTROL_ENABLED`，否则会因 access 缺 key 启动失败）；
  - `verify_llm_quota_cli.py`：**59 checks PASS**。
- **前端记录**：F19 分类/文件导入 **158 checks PASS**；F20 access UI **58 checks PASS**（2026-10-23～25 记录，headless）。
- **仍未验收**：真实 Provider、真实 LAN、真实手机；access 开启后的真实网关与业务页面回归。

> 说明：环境变量作用域问题（用户级 `TMF_ACCESS_CONTROL_ENABLED=false`、当前父进程环境为 `true`）属于运行环境差异，不是 4B 断言失败，详见根 [README](../README.md) 与 v7 学习档案附录 D。
