# TouManFen：中文新闻标题十分类工程化平台

[![Python](https://img.shields.io/badge/Python-3.10--3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.150.0-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Vue-3.5-42b883?logo=vue.js&logoColor=white)](https://vuejs.org/)
[![Node](https://img.shields.io/badge/Node-24-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![GitHub](https://img.shields.io/badge/GitHub-cwenxuan815--tech%2FTouManFen-181717?logo=github)](https://github.com/cwenxuan815-tech/TouManFen)

这是一个面向中文新闻标题自动归类需求的中文短文本多分类项目。系统覆盖数据准备、传统模型基线、BERT 微调、大模型零样本分类、知识蒸馏、动态量化、剪枝，以及统一 FastAPI 网关、5 个独立模型 worker、Vue 3 前端和离线评估/演示链路。

> 项目采用代码、数据与模型资产分离的交付方式。完整数据集、预训练模型和训练权重由部署环境独立管理，不随 Git 提交；仓库内仅保留可复现的代码、配置、文档和小体积证据产物。

更完整的工程复盘与决策记录见 [v6 全链路交付与隔离验收](docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md)，指标口径见 [实测指标](docs/实测指标.md)，接口变更见 [接口变更记录](docs/接口变更记录_2026-10-21.md)。

## 导航

- [项目解决的问题](#解决的问题)
- [快速了解](#快速了解)
- [系统架构](#系统架构)
- [核心能力](#核心能力)
- [数据处理链路](#数据处理链路)
- [模型体系与技术选型](#模型体系与技术选型)
- [训练评估与模型优化](#训练评估与模型优化)
- [推理与API服务](#推理与api服务)
- [项目结构与关键文件](#项目结构与关键文件)
- [环境准备与运行方式](#环境准备与运行方式)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [评估口径](#评估口径)
- [测试与工程质量](#测试与工程质量)
- [部署与资产管理](#部署与资产管理)
- [项目交付成果](#项目交付成果)
- [许可证](#许可证)

## 解决的问题

- 新闻标题人工归类效率低，批量内容难以快速投递到正确频道；
- 多个模型（随机森林、fastText、BERT、LLM、蒸馏模型）使用不同入口和口径，难以横向比较；
- 深度模型在 CPU 上体积大、延迟高，需要通过量化、蒸馏、剪枝评估部署取舍；
- 多模型服务缺少统一编排、健康探测、超时/错误归一化和请求追踪；
- 模型服务直接对外暴露时，演示环境容易出现 mock/live 混淆、离线不可用和结果不可追溯；
- 训练产物、模型权重、数据和密钥容易误入 Git 仓库。

## 快速了解

| 业务需求或工程问题 | 解决方案 | 形成的交付能力 |
|---|---|---|
| 新闻标题需要自动投递到 10 个频道 | 建立中文短文本十分类链路 | 单条分类、多模型对比、SSE 渐进对比 |
| 不同模型训练/评估口径不一致 | 固定独立 test 集与统一 Macro 指标 | RF / fastText / BERT / 蒸馏 / LLM 可同口径比较 |
| CPU 部署成本高 | 动态 INT8 量化、BiLSTM 蒸馏、非结构化剪枝 | 体积、延迟、精度取舍有实测数据 |
| 多模型服务入口分散 | FastAPI 网关 + `ModelAdapter` 协议 | 前端只访问网关，worker 独立进程隔离 |
| worker 行为不一致 | `tmf_common.PredictService` 模板 | 5 个模型统一加载、校验、推理、白名单与错误格式 |
| 演示/联调容易把 mock 当真实结果 | `data_source`、`/demo/state`、cached 快照 | live/mock/unknown/cached 可区分、可展示来源 |
| 反馈和人工偏好无处沉淀 | SQLite 反馈/竞技场接口 | 反馈 pending、去重、过期清理；Elo + Wilson 榜 |
| 大文件/密钥容易进入 Git | `.gitignore`、资产外置、提交前扫描 | 代码与数据/模型分离，仓库可公开维护 |

项目的核心价值不是单一模型训练脚本，而是把新闻分类能力整理为可维护的工程系统：数据入口、5 个模型、统一适配层、服务编排、前端交互、离线评估、演示真实性与资产边界均由明确模块管理。

## 系统架构

```text
业务数据 / 训练脚本（离线）
    │
    ├── 00-data：train/dev/test/class/stopwords
    ├── 01-rf：jieba + TF-IDF + RandomForest
    ├── 02-fasttext：字符级/词级 × 手动/自动
    ├── 03-bert：BERT 全量微调 → 动态量化
    ├── 04-LLM：外部大模型零样本
    └── BERT 产物 → 05-distill（BiLSTM）/ 06-prune（30% 剪枝）

在线链路
浏览器
  │  http://127.0.0.1:5173
  ▼
Vue 3 + TypeScript + Vite（frontend/）
  │  /api/v1/*
  ▼
FastAPI 统一网关（serving/gateway，:8000）
  │  业务层只依赖 ModelAdapter 协议
  ▼
serving/adapters/
  ├── HttpWorkerAdapter ──HTTP──> Flask worker :8101 RF
  │                              Flask worker :8103 fastText
  │                              Flask worker :8104 BERT
  │                              Flask worker :8106 蒸馏 BiLSTM
  └── LlmAdapter ────────HTTP──> Flask worker :8105 LLM → 外部 Provider

旁路/支撑
  ├── SQLite：反馈、竞技场投票（serving/run/tmf.db，本地）
  ├── artifacts/bench：离线 PK 基准（只读 API）
  ├── artifacts/demo：演示快照（前端 cached 回退）
  └── artifacts/d4 等：验收证据
```

网关只通过 HTTP 调用 worker，**不 import torch/transformers、不加载模型、不读取密钥**。5 个 worker 保持独立进程，以隔离 numpy/transformers/torch/OpenAI SDK 等依赖和故障；`tmf_common` 提供零重依赖的共享契约与网络配置。

## 核心能力

| 模块 | 仓库状态 | 边界 |
|---|---|---|
| 数据与 10 类标签 | 已实现 | 数据由部署环境提供，不入库 |
| 随机森林基线 | 已实现并实测 | 独立 test macro-F1 0.8302 |
| fastText（4 模型） | 已实现并实测 | 服务端加载词级 auto，test 0.9173 |
| BERT 全量微调 | 已实现并实测 | test 0.9307，CPU 1 epoch 约 5h20m |
| LLM 零样本分类 | 已实现；**真实 Provider 未系统验证** | 默认 opt-in；离线基准 missing |
| 蒸馏 BiLSTM v1/v2/v2b | 已实现并实测 | v2b test 0.9074，36.7 MB |
| 动态量化 / 剪枝 | 已实现并实测 | 量化在线集合未启用；剪枝体积不变 |
| FastAPI 网关 + 适配层 | 已实现并验证 | 单模型、对比、SSE、反馈、竞技场、基准、演示状态 |
| Vue 3 前端 | 已实现并 headless 验证 | 分类/对比/SSE/反馈/竞技场/PK/主题/大屏/离线快照 |
| 自动化验收 | 已实现并执行 | 本轮验证项数见“测试与工程质量” |
| 真实设备/真实 LLM | **未验证** | 仅隔离 mock/headless 环境；D7 未执行 |

### 当前状态与边界

- 当前定位是**本地/局域网验证型工程**，不是生产系统：无鉴权、无 HTTPS、无用户体系、无生产级数据库/迁移、无容器化；
- 反馈/投票仅存本地 SQLite，重启/清理策略为 30 天惰性清理；
- 离线基准中 **LLM 为 missing**（外部 Provider 未授权），不得用 mock/估算分数补齐；
- 竞技场榜单是**人类偏好**（Elo + Wilson），不是模型准确率；
- mock/cached 数据不得标记为 live；真实设备、真实 LAN 断开、真实 LLM 未验收。

## 数据处理链路

### 输入合同

数据格式为每行 `标题<TAB>标签`，标签来自 `00-data/data/class.txt`（10 类，行序即类别 ID）：

```text
finance, realty, stocks, education, science,
society, politics, sports, game, entertainment
```

默认规模：

| 文件 | 行数 | 用途 |
|---|---|---|
| `train.txt` | 180,000 | 训练（10 类 × 18,000） |
| `dev.txt` | 10,000 | 验证/调参 |
| `test.txt` | 10,000 | 独立评估 |
| `class.txt` | 10 | 类别唯一来源 |

### 清洗与口径

- 主口径为**未清洗的 180,000 条**训练数据；
- 附加清洗实验：180,000 → **178,692**（-0.73%：重复标题 809、标签冲突 96、与 dev/test 重叠 403），报告见 `00-data/data/clean_report.md`；
- 蒸馏 v2b 是唯一使用清洗集训练的服务端模型，已在文档中单独标注；
- 各模块 `data/` 副本由 `python tools/prepare_data.py` 生成，不入库。

### 标签与评估隔离

- 类别顺序来自 `class.txt`，训练、评估、worker 白名单共用同一来源；
- 评估使用独立 `test.txt`，避免与训练/调参混用；
- 类别均衡（10 × 18,000 / 10 × 1,000），主指标为 Macro-F1。

## 模型体系与技术选型

| 模型 | 选型目的 | 工程实现 |
|---|---|---|
| TF-IDF + RandomForest | 快速、可解释的传统基线 | scikit-learn + jieba 分词 |
| fastText | 高效浅层文本分类 | 字符级/词级、手动/自动调参，共 4 个模型 |
| BERT | 中文上下文语义基线 | `bert-base-chinese` 全量微调（B 方案） |
| DeepSeek / Qwen / 智谱 | 零样本大模型对照 | `openai` 兼容 SDK；默认 DeepSeek，可切换 |
| 蒸馏 BiLSTM | CPU 轻量部署 | BERT 教师 → BiLSTM 学生 v1/v2/v2b |
| 动态量化 | CPU 模型压缩 | 仅 Linear 层动态 INT8（训练后量化） |
| 非结构化剪枝 | 稀疏化实验 | 注意力 q/k/v 全局 30% 剪枝 |

服务端模型 ID 固定为 `rf`、`fasttext`、`bert`、`llm`、`distill`；量化/剪枝模型不在当前在线集合。

## 训练评估与模型优化

### 训练与产物

训练脚本按模块独立运行，`run_all.py` 是按阶段调用各模块脚本的骨架（不含服务启动）：

```powershell
python run_all.py --list
python run_all.py --stage rf
```

主要实测耗时（CPU）：

| 步骤 | 耗时 | 产物 |
|---|---|---|
| RF 预处理 + 训练 | 23.4 s + 628.3 s | `rf.pkl`（1,512 MB）+ `tfidf.pkl` |
| fastText 预处理 + 4 模型 | 31.8 s + 528.0 s | 4 个模型合计 2.49 GB |
| BERT 全量微调（1 epoch） | 5 h 20 m | `bert.pkl`（390.2 MB）+ 断点 |
| 动态量化 | 545.7 s | `quantization_model.pkl`（152.7 MB） |
| 蒸馏 v1 / v2 / v2b | 2 h 13 m / 2 h 22 m + 缓存 / 40 m | 26.6 MB / 36.7 MB |
| 剪枝 | 705.3 s | before/after 各 390.2 MB |

### 统一评估指标

独立 test 集 10,000 条、主口径：

| 模型 | 体积 | macro-F1 | 1 万条推理 | 定位 |
|---|---|---|---|---|
| 随机森林 | 1,512 MB | 0.8302 | 1.2 s | 基线/最低延迟 |
| fastText 词级 auto | 451 MB | 0.9173 | 秒级 | 词向量基线 |
| **BERT fp32** | 390.2 MB | **0.9307** | 339 s | 精度最高 |
| BERT 量化 int8 | **152.7 MB** | 0.9263 | **120 s** | 性价比最高 |
| BERT 剪枝 30% | 390.2 MB | 0.9294 | 336 s | 精度近无损、体积不变 |
| 蒸馏 BiLSTM v2b | **36.7 MB** | 0.9074 | **13 s** | 最轻量（清洗集） |

### 优化模块

- **知识蒸馏**：学生交叉熵 + 教师软标签 KL 散度 + 温度平方补偿；v1 失败后通过词嵌入 SVD 初始化、教师 logits 缓存和清洗集续训提升到 v2b；
- **动态量化**：CPU 推理场景，仅 Linear 层 INT8，量化后体积 152.7 MB、约 2.8× 提速；
- **剪枝**：非结构化 30%，精度约 -0.13，但固化后仍是稠密存储，**不减小体积**；
- **LLM**：预算/重试/白名单在 worker 内，未做全量准确率/成本评估。

## 推理与API服务

### 统一网关

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/api/v1/healthz` | 网关进程存活 |
| `GET` | `/api/v1/readyz` | 5 个 worker 就绪汇总 |
| `GET` | `/api/v1/models` | 模型清单、默认模型、10 类、状态 |
| `GET` | `/api/v1/stats` | 进程内请求/模型调用统计 |
| `POST` | `/api/v1/classify` | 单模型分类（默认 fasttext） |
| `POST` | `/api/v1/compare` | 多模型对比（允许部分失败） |
| `GET` | `/api/v1/compare/stream` | SSE 渐进对比（model/summary/done） |
| `POST` | `/api/v1/feedback` | 提交纠正反馈（pending、去重） |
| `GET` | `/api/v1/feedback/stats` | 反馈聚合统计 |
| `GET` | `/api/v1/arena/next` | 匿名抽题 |
| `POST` | `/api/v1/arena/vote` | 匿名投票 |
| `GET` | `/api/v1/arena/leaderboard` | Elo + Wilson 排行榜 |
| `GET` | `/api/v1/bench/pk` | 离线基准最新产物（只读） |
| `GET` | `/api/v1/demo/state` | 启动数据来源（live/mock/unknown） |

单模型请求：

```json
{
  "title": "北京二手房成交量创年内新高",
  "model": "bert"
}
```

成功响应：

```json
{
  "request_id": "...",
  "model_id": "bert",
  "model_version": null,
  "pred_class": "realty",
  "latency_ms": 70
}
```

### 模型 worker

| 方法 | 路径 | 功能 |
|---|---|---|
| `POST` | `/predict_api` | `{"title": "..."}` → `{"title": "...", "pred_class": "sports"}` |
| `GET` | `/healthz` | 进程存活 |
| `GET` | `/readyz` | 模型/客户端就绪（LLM 不验证密钥） |
| `GET` | `/metadata` | `model_id` / 展示名（用于 live/mock 判定） |

- `api_flask_server.py` 是薄壳；5 个模型共用 `tmf_common/flask_shell.py` 与 `PredictService` 模板；
- 各模型保留自己的预处理（jieba / TF-IDF / BERT tokenizer / BiLSTM / LLM Prompt）；
- 网关统一处理超时、错误码、request ID、CORS 与统计。

### 反馈、竞技场与演示真实性

- 反馈只写 `pending`，支持去重和 30 天清理，不直接参与训练；
- 竞技场投票匿名，排行榜是**人类偏好**而非准确率；
- `data_source` 由 worker `/metadata.display_name` 判定：`mock-` 前缀=mock，正常=live，缺失/不可达=unknown；
- 前端在 `/demo/state` 不可用时读取 `frontend/public/snapshot.json`，显示“离线快照”，恢复后退出 cached。

## 项目结构与关键文件

```text
.
├── 00-data/                 数据与类别定义
├── 01-rf/                   随机森林：预处理/训练/预测服务/薄 Flask
├── 02-fasttext/             fastText 四模型
├── 03-bert/                 BERT 微调/量化
├── 04-LLM/                  大模型零样本与 Provider 配置
├── 05-distill/              教师/学生蒸馏
├── 06-prune/                剪枝实验
├── tmf_common/              共享预测契约与网络配置（零重依赖）
├── serving/
│   ├── adapters/            ModelAdapter / HttpWorkerAdapter / LlmAdapter
│   ├── gateway/             FastAPI 应用、routers、schemas、errors、stats
│   ├── services/            分类编排、反馈、竞技场
│   ├── config/              workers/models/processes YAML
│   └── scripts/             启停、mock、验收与测量脚本
├── frontend/
│   ├── src/                 Vue 组件、composables、API 客户端、样式
│   ├── public/snapshot.json cached 演示快照
│   └── tests/               F9/F10/F11/F14/F15 headless 测试
├── tools/                   数据、基准、负样本、反馈清理等工具
├── artifacts/               基准/并发/D4 隔离/演示证据（小体积）
├── docs/                    学习档案、指标、接口、彩排、验收清单
├── run_all.py               训练流水线骨架
├── 启动TMF.bat / 停止TMF.bat Windows 一键启停
└── README.md
```

关键入口：

| 文件 | 作用 |
|---|---|
| `serving/gateway/main.py` | 应用装配、CORS、request ID、适配器 composition root |
| `serving/adapters/protocol.py` | `ModelAdapter` 协议与结果类型 |
| `serving/services/classification.py` | 单模型/对比编排、超时与失败隔离 |
| `tmf_common/prediction.py` | 统一预测模板与兼容 JSON |
| `tmf_common/config.py` | host/port/URL/CORS 环境变量解析与校验 |
| `serving/config/workers.yaml` | worker URL、路径、超时、启用状态 |
| `frontend/src/App.vue` | 单页应用入口与页面编排 |
| `frontend/src/api/` | 分类、对比、SSE、反馈、竞技场、基准 API 客户端 |
| `serving/scripts/verify_*.py` | 契约、接口、功能与隔离验收 |
| `docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md` | 最新工程复盘与决策记录 |

## 环境准备与运行方式

### 前置条件

| 项 | 要求 |
|---|---|
| OS | Windows 10/11（一键启停脚本为 Windows 专属；核心 Python 服务可移植） |
| Python | 3.10–3.12，本项目实测 3.10.9 |
| 网关依赖 | `serving/requirements-gateway.txt`；本机使用同级 `venv-gateway` 注入 |
| Node / pnpm | Node 24 LTS + pnpm 11 |
| 模型资产 | `save_model/` 下的 RF/fastText/BERT/蒸馏产物；`../PretrainedModel/bert-base-chinese` |
| 数据 | `00-data/data/{train,dev,test,class,stopwords}.txt` |

> 仓库不包含数据、模型权重和预训练模型。缺少资产时后端会在启动校验阶段失败，这是预期行为。

### 三条版本约束

| 约束 | 原因 |
|---|---|
| `numpy<2` | fastText 的 `np.array(..., copy=False)` 在 NumPy 2.x 抛异常 |
| `transformers<5` | 5.x 移除 `tokenizer.batch_encode_plus` |
| `fasttext-wheel` 而非 `fasttext` | Windows 无官方预编译轮子 |

```powershell
pip install -r requirements.txt
cd frontend
pnpm install
```

## 快速开始

### 1. 准备资产

将数据放入 `00-data/data/`，预训练 BERT 放到 `../PretrainedModel/bert-base-chinese/`，并按需训练/放置 `save_model/` 产物（训练见下文）。不要把数据、权重或 `.env` 提交到 Git。

### 2. 启动后端与前端

```powershell
# 一键启动（Windows，自动选择当前默认路由 IPv4；手机和电脑需在可互访的局域网）
启动TMF.bat

# 一键停止前后端
停止TMF.bat

# 或手动
powershell -NoProfile -ExecutionPolicy Bypass -File serving\scripts\stage2.ps1 -Action start
cd frontend
pnpm dev
```

默认访问：`http://127.0.0.1:5173`，网关 `http://127.0.0.1:8000`。

### 3. 局域网访问

`启动TMF.bat` 会发现本机活动 Wi-Fi/以太网接口上的私有 IPv4 地址，将前端和网关绑定到所有网卡、将 worker 保持在本机回环地址，并为各地址配置 CORS。前端 API 按当前浏览器访问页面的 hostname 选择同地址网关，因此多网卡/多网段可分别使用各自 URL。脚本会打印所有检测到的 URL。若需手动指定，可在启动前设置 `TMF_LAN_IPS` 为逗号分隔的本机 IPv4 地址列表；`TMF_LAN_IP` 仍可指定单个地址。

脚本会等待 Vite 就绪，并在已有前端时核对各 LAN 地址上的页面、动态 API 地址解析和网关健康状态后复用。两个网段的设备能否访问，还取决于电脑确实拥有对应 IP，或路由器允许两个子网互相路由；不同 Wi-Fi/VLAN 可能启用设备隔离。`04-LLM/.env` 未配置非空 Key 时，stage2 会按设计跳过 LLM worker，其余已配置服务仍可启动。`停止TMF.bat` 停止状态文件记录的进程，并按 worker 专属启动标记回收状态文件缺失或遗漏的 TMF worker；网关和 Vite 还须同时匹配 TMF 命令行与对应监听端口。脚本最后检查 5173/8000/worker 端口，仍有监听时会报告端口和 PID，不会仅凭端口强杀进程。

手动启动时，配置如下（将 `<LAN_IP>` 换成电脑当前 Wi-Fi IPv4 地址）：

```powershell
$env:TMF_FRONTEND_HOST='0.0.0.0'
$env:TMF_GATEWAY_HOST='0.0.0.0'
$env:TMF_WORKER_HOST='127.0.0.1'
$env:VITE_API_PORT='8000' # API hostname follows the page hostname
$env:TMF_CORS_ORIGINS='http://<LAN_IP_1>:5173,http://<LAN_IP_2>:5173'
```

前端用 `pnpm dev` 启动后，各网段客户端访问对应网卡地址的 `http://<LAN_IP>:5173`；页面会访问同 hostname 的 `:8000` 网关。Windows 防火墙只需在相关网络配置文件放行 TCP 5173（前端）和 8000（网关）；worker 端口保留本机访问，不要向局域网开放。`0.0.0.0` 仅是监听地址，不能作为浏览器连接目标。同一 Wi-Fi 通常处于同一局域网，但访客网络、AP/客户端隔离或 VLAN 可能阻止设备互访。

### 4. 训练（可选）

```powershell
python run_all.py --list
python run_all.py --stage rf
# 或进入模块目录单独运行训练/量化/蒸馏/剪枝脚本
```

### 5. 验收

```powershell
# 契约与适配层（不加载模型）
python serving/scripts/verify_refactor_gateway.py
python serving/scripts/verify_refactor_workers.py

# 真实服务（先 start）
python serving/scripts/verify_stage2.py
python serving/scripts/verify_stage3.py --llm-mode skip

# 前端
cd frontend
pnpm type-check
pnpm build
```

## 配置说明

### 配置文件

| 文件 | 作用 |
|---|---|
| `serving/config/workers.yaml` | worker URL、路径、超时、启用状态 |
| `serving/config/models.yaml` | 模型 ID、默认模型、类别来源、compare 默认集合 |
| `serving/config/processes.yaml` | Windows 启停脚本使用的 host/port |
| `frontend/vite.config.ts` | 前端监听地址/端口 |
| `frontend/src/api/client.ts` | API 基址 |

### 关键环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `TMF_BIND_HOST` | 5 个 worker 监听地址 | `127.0.0.1` |
| `TMF_GATEWAY_HOST` / `TMF_GATEWAY_PORT` | 网关监听地址/端口 | `127.0.0.1` / `8000` |
| `TMF_FRONTEND_HOST` / `TMF_FRONTEND_PORT` | 前端监听地址/端口 | `127.0.0.1` / `5173` |
| `TMF_WORKER_HOST` / `TMF_WORKER_URL_<ID>` | 网关调用 worker 的主机/完整 URL 覆盖 | 取 YAML |
| `TMF_CORS_ORIGINS` | 追加 CORS 来源（禁止 `*`） | 由前端地址推导 |
| `TMF_DB_PATH` | 反馈/竞技场 SQLite 路径 | `serving/run/tmf.db` |
| `TMF_BENCH_DIR` | 离线基准产物目录 | `artifacts/bench` |
| `TMF_LLM_PROVIDER` | `deepseek` / `qwen` / `zhipu` | `deepseek` |
| `TMF_LLM_TOTAL_TIMEOUT_S` | LLM 上游总预算 | 12 s |
| `VITE_API_BASE_URL` | 前端 API 基址 | `http://127.0.0.1:8000` |
| `VITE_API_PORT` | 页面 hostname 动态 API 模式使用的网关端口 | `8000` |
| `TMF_BERT_PATH` | 预训练 BERT 目录 | `../PretrainedModel/bert-base-chinese` |

## 评估口径

- 主评估集：独立 `test.txt` 10,000 条，10 类均衡；
- 统一指标：Accuracy、Macro Precision、Macro Recall、Macro-F1、分类报告、混淆矩阵；
- 模型体积/延迟为固定 CPU 环境实测，换硬件/线程/输入长度会变化；
- 离线 PK 基准绑定数据快照、模型版本、指标定义与 `data_kind`，只代表离线评估，不是实时线上准确率；
- 竞技场是匿名人类偏好，不是准确率；
- 反馈为 pending 纠正记录，不参与训练；
- LLM 未完成全量评估，B8 基准中为 missing，不得用 mock/估算值替代。

## 测试与工程质量

### 验证快照（2026-10-22，隔离/本地环境）

| 检查项 | 结果 | 证据边界 |
|---|---|---|
| 网关适配层 | `verify_refactor_gateway.py` **101 PASS** | 配置/协议/API/CORS/错误路径 |
| worker 契约 | `verify_refactor_workers.py` **93 PASS** | 5 个服务兼容与静态检查 |
| stage3 mock | `verify_stage3.py --llm-mode mock --isolate-mock` **59 PASS** | 隔离端口/DB，未访问真实 LLM |
| B9 LLM opt-in | **16 PASS** | 默认 compare 不含 LLM |
| B11 数据来源 | **39 PASS** | mock/live/unknown、统计只计 live |
| F14 前端 | 6 场景 / **66 断言** | headless，含 reduced-motion/CPU 节流 |
| D4 隔离彩排 | 10/10 + 5 个故障场景 | mock/headless，非真实设备 |
| 前端构建 | `pnpm type-check` + `pnpm build` 通过 | 当前依赖版本 |

### 当前工程约束

- 本仓库**没有配置 GitHub Actions CI**；上述为本地隔离验证结果；
- 真实 LLM、真实投影/手机/笔记本、真实 LAN 断开未验收；
- 未对用户 stage2 服务做压测/启停操作；
- 测试产物保存在 `artifacts/` 与 `frontend/tests/**/artifacts`，作为小体积证据随仓库提交。

## 部署与资产管理

- **数据**：企业/本地数据平台提供，放 `00-data/data/`，不提交；
- **预训练模型**：`../PretrainedModel/`，不入库；
- **训练权重/检查点**：`save_model/`，不入库；
- **密钥**：`04-LLM/.env`、`frontend/.env.local`，不入库；
- **反馈数据库**：`serving/run/tmf.db`，本地 SQLite，不入库；
- **日志/运行态**：`serving/logs/`、`serving/run/`，不入库；
- **证据产物**：`artifacts/` 与前端测试截图是小型验收证据，已随仓库提交；
- **服务暴露**：当前无鉴权/HTTPS/TLS/限流，局域网使用需自行加反向代理与访问控制。

## 项目交付成果

- 完成数据准备、10 类标签、独立 test 集与清洗实验；
- 完成 RF、fastText、BERT、LLM、蒸馏、量化、剪枝的全链路训练与评估；
- 建立 `ModelAdapter` 协议与 5 个独立 worker 的统一 `PredictService` 契约；
- 建立 FastAPI 网关、Vue 3 前端、单条/对比/SSE/反馈/竞技场/PK/演示能力；
- 建立 `data_source`、`/demo/state`、cached 快照的演示真实性机制；
- 建立 SQLite 反馈/投票、30 天清理、匿名榜单与离线基准；
- 建立本地/局域网可配置网络、超时/错误归一化、request ID 与统计；
- 建立自动化验收脚本、headless 前端测试与隔离彩排；
- 代码、数据、模型权重、密钥与证据产物边界清晰，可公开维护。

面试介绍时可以概括为：

> 围绕中文新闻标题十分类需求，我完成了从数据准备、传统模型与 BERT/LLM/蒸馏/量化/剪枝，到统一适配层、FastAPI 网关、Vue 前端和离线评估的完整链路；并通过独立进程隔离、统一 worker 契约、可配置网络、数据来源标识、反馈/竞技场和隔离验收，解决了多模型服务编排、模型结果口径不一致、演示真实性与资产边界等工程问题。

## 许可证

仓库暂未添加开源许可证。公开发布前需要确认代码、数据、课程资料和衍生模型的权属与授权范围；当前仓库为 public，如仅用于内部验证建议改为 private。
