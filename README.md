# 投满分新闻分类项目（TouManFenClassifier）

把中文新闻标题自动分到 10 个频道（`finance, realty, stocks, education, science, society, politics, sports, game, entertainment`），覆盖一条完整链路：

> 数据准备 → 随机森林基线 → fastText → BERT 微调 → 大模型（DeepSeek）→ 模型压缩（量化 / 蒸馏 / 剪枝）→ 统一服务层（FastAPI 网关 + 模型适配层）→ Vue 前端

项目已从「课程参考代码」整理为一个可训练、可评估、可服务、可局域网访问的验证型工程。**当前定位是本地/局域网验证环境，不是生产系统**（无鉴权、无 HTTPS、仅本地 SQLite 存反馈/投票、无生产级数据库/迁移、无容器化，详见「已知限制」）。

> **文档状态（2026-10-22 最终）**：在适配层重构之上完成了 D5 SSE、D6 B8/B12/F10/F11，以及最终收尾 B9/B10/B11/D1/D2/F14、D3/D4、F15 启动来源与缓存回退。学习档案新增 **v6**（v1–v5 保留为历史/前期归档）。历史文档中的固定 IP、旧架构和旧验收状态仅作历史记录，最新口径以本 README、`docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md` 与各专项文档为准。
>
> **最终交付状态（2026-10-22）**：单模型/对比/SSE/反馈/竞技场/PK 榜/演示快照已实现；B8 的 **LLM 仍为 missing**（未授权真实 Provider），D4 已在隔离 mock/headless 环境完成 10 步正常流程与 5 个故障场景复验；真实设备、真实 LAN 断开、真实 LLM 未验收；D7 未执行。**用户 stage2 服务未由本项目测试启动/停止/压测**；本轮改动仍未 commit（HEAD 沿用既有提交）。

---

## 一、当前状态与交付能力

| 能力 | 状态 | 关键结果 |
|---|---|---|
| 数据集（18 万 / 1 万 / 1 万，10 类） | ✅ | `00-data/data/`，独立 test 集评估 |
| 随机森林基线 | ✅ | test macro-F1 **0.8302**，1,512 MB |
| fastText（4 个模型） | ✅ | 最优 **0.9173**（词级 auto，451 MB，服务端加载） |
| BERT 全量微调（B 方案） | ✅ | test macro-F1 **0.9307**，390.2 MB，CPU 5h20m |
| 动态量化 int8（DQ） | ✅ | **0.9263**，152.7 MB（2.68× 压缩），2.8× 提速 |
| 非结构化剪枝 30% | ✅ | **0.9294**（-0.13），体积不变（结论已按实测修正） |
| 蒸馏 BiLSTM（v1 / v2 / v2b） | ✅ | v2b **0.9074**，36.7 MB，13 s/万条（10.6× 压缩） |
| 大模型分类（DeepSeek） | ✅ 已实现；部分验证 | 零样本 + 10 类白名单校验；2026-10-19 Streamlit 联调、2026-10-20 16:22–18:06 经网关的 16 次 `model_call=ok`（0.5–4.0 s）均有真实调用记录；验证脚本只用 mock；未做准确性/成本/稳定性系统评估 |
| 5 个 Flask 模型服务 | ✅ | 8101/8103/8104/8105/8106，统一 `POST /predict_api`；入口为薄壳，共用 `tmf_common.PredictService` 契约 |
| FastAPI 统一网关 | ✅ | 8000；模型清单、健康探测、单模型分类、多模型对比、统计 |
| 统一模型适配层 | ✅ | `serving/adapters/`：`ModelAdapter` 协议 + `HttpWorkerAdapter` / `LlmAdapter`；业务层只依赖协议 |
| 统一 worker 预测契约 | ✅ | 5 个模型统一「加载 → 标题校验 → 推理 → 白名单校验 → 结果」；保留旧 `predict(dict)`/`/predict_api` 兼容 |
| Vue 3 单页前端 | ✅ 已实现；headless/CDP 已验证 | 5173；分类/对比/SSE/反馈/竞技场/PK/主题/大屏/F15 来源；未做真实投影/手机人工视觉验收 |
| 本地/LAN 可配置地址 | ✅ | 默认 `127.0.0.1`；LAN 通过环境变量覆盖（`TMF_BIND_HOST` 等），运行路径无固定 IP |
| 一键启停（Windows） | ✅ | `启动TMF.bat` / `停止TMF.bat`，可重复执行；后端 stop 于 2026-10-20 18:17 经日志确认 |
| 自动化验收脚本 | ✅ 已实现；已验证 | `verify_refactor_gateway.py` / `verify_refactor_workers.py` / `verify_stage2.py`（35 项）/ `verify_stage3.py`；2026-10-21 重构后均 RESULT PASS |
| SSE 渐进对比 | ☑ 已实现；已验证 | `/api/v1/compare/stream`；逐项 model/summary/done；总截止 18000ms、断连清理；B6 67 PASS |
| B8 离线 PK/LLM | ◐ 部分完成 | test 10,000；RF/fastText/BERT/distill + 派生集成/oracle；**LLM missing（未授权）**；B8 65 PASS |
| B9 LLM opt-in | ☑ 已实现；已验证 | 默认 compare 不含 LLM；显式选择才请求；16 PASS |
| B10/B11/B12 集成 | ☑ 已实现；已验证 | gateway 101 / workers 93 / stage3 mock 59；data_source；并发 3、threads 4、锁；B12 89 PASS |
| 演示/彩排（D2/D4/F15） | ☑ 已实现；验证为隔离环境 | 8 演示标题 + 20 题 + cached 快照；启动来源 `/demo/state`；D4 隔离 10/10 + 5 故障；真实设备/LLM 未验收 |

**主口径**：除蒸馏 v2b（清洗集训练，唯一例外）外，服务端模型均由**未清洗的 180,000 条**训练；量化 / 剪枝由 BERT 派生。指标为独立 `test.txt`（1 万条）实测，详见 [docs/实测指标.md](docs/实测指标.md)。

---

## 二、技术栈

| 层 | 技术选型 |
|---|---|
| 前端 | Vue `3.5.44` + TypeScript `5.9.3` + Vite `8.3.4`（rolldown）+ `vue-tsc 3.3.13`；原生 `fetch`，无路由/状态库/UI 库 |
| 统一网关 | Python 3.10.9 + FastAPI `0.150.0` + Starlette `1.7.0` + **Pydantic v2** + httpx `0.28.1` + Uvicorn `0.54.0` + PyYAML `6.0.3` |
| 模型适配层 | `serving/adapters/`：`ModelAdapter` 协议 + `HttpWorkerAdapter` / `LlmAdapter`；网关业务层只依赖协议，适配器不加载模型、不读密钥 |
| 模型服务 | Flask `3.1.3` 单进程（`debug=False`），每模块一个薄入口 `api_flask_server.py` + 统一 `PredictService`（`tmf_common/`） |
| 大模型 | `openai>=1.0` SDK（OpenAI 兼容协议）→ DeepSeek `deepseek-chat`，可切 Qwen / 智谱；`python-dotenv` 管理密钥 |
| 算法 | torch `2.14.1+cpu`、transformers `4.57.6`（`bert-base-chinese`）、scikit-learn `1.7.2`、fasttext-wheel `0.9.2`、jieba、pandas、numpy |
| 旧版页面 | 各模块仍保留 Streamlit `app_streamlit.py`（`py3109` 环境未安装 Streamlit，主前端为 Vue） |
| 运行环境 | Windows 11、CPU-only（Ryzen 7 8845H / 31.3 GB）、Python 3.10.9（conda `py3109`）、Node 24 + pnpm 11、`venv-gateway` 网关依赖目录 |

---

## 三、系统架构

### 3.1 运行拓扑（当前默认，本地单机）

```text
浏览器
  │  http://127.0.0.1:5173
  ▼
Vite 开发服务器 :5173（Vue 3 + TypeScript SPA）
  │  fetch  /api/v1/models · /classify · /compare
  ▼
FastAPI 统一网关 :8000（serving/gateway，不 import torch/transformers）
  │  业务层只依赖 ModelAdapter 协议；composition root 装配适配器
  ▼
serving/adapters/
  ├── HttpWorkerAdapter ──HTTP──> Flask worker :8101 随机森林
  │                              Flask worker :8103 fastText
  │                              Flask worker :8104 BERT
  │                              Flask worker :8106 蒸馏 BiLSTM
  └── LlmAdapter ────────HTTP──> Flask worker :8105 大模型 → DeepSeek / Qwen / 智谱（外部 API）
        （06-prune 无在线服务，仅离线实验）
```

- 前端只访问网关，不直接访问 worker；worker 地址、超时、错误归一化由网关统一管理。
- 网关通过 `serving/config/workers.yaml` 调用 worker，通过 `models.yaml` 维护模型清单。
- 5 个 worker 仍以独立进程运行（依赖隔离/故障隔离）；`api_flask_server.py` 只是薄壳，路由/校验/白名单/错误格式由 `tmf_common.flask_shell` + `PredictService` 统一。
- 监听地址与端口默认 `127.0.0.1`，局域网通过环境变量覆盖（见 5.5），源码/配置中不写死某台机器的 IP。
- 进程由 `serving/scripts/stage2.ps1` 管理，状态写入 `serving/run/stage2-state.json`，日志在 `serving/logs/`。

### 3.2 训练 / 实验链路

```text
00-data（train/dev/test/class/stopwords）
   ├── 01-rf         jieba + TF-IDF + RandomForest
   ├── 02-fasttext   字符级/词级 × 手动/自动调参（4 个模型）
   ├── 03-bert       bert-base-chinese 全量微调（B 方案）
   │     └── 模型量化_DQ.py（动态量化 int8）
   ├── 04-LLM        外部大模型零样本分类（需 .env 密钥）
   └── BERT 产物 ──► tools/handoff_models.py
                       ├── 05-distill（BERT 教师 → BiLSTM 学生 v1/v2/v2b）
                       └── 06-prune（注意力 q/k/v 全局 30% 非结构化剪枝）
```

---

## 四、目录结构

```text
TouManFenClassifier/
├── 00-data/
│   ├── data/                 train.txt(18万) dev.txt(1万) test.txt(1万)
│   │                         class.txt stopwords.txt train.clean.txt clean_report.md
│   └── eda/                  config.py  data_eda.py
├── 01-rf/                    随机森林：config / 预处理 / 训练 / 预测服务（统一契约）/ 薄 Flask / Streamlit
├── 02-fasttext/              fastText 四模型 + 预测服务 / 薄 Flask / Streamlit
├── 03-bert/                  BERT 微调、评估、预测服务、量化（模型量化_DQ.py）、log/
├── 04-LLM/                   DeepSeek/Qwen/智谱脚本、llm_predict_service、薄 Flask、.env(.example)
├── 05-distill/               教师/学生模型、model_distll.py(v1)、distill_v2.py(v2/v2b)、cache/
├── 06-prune/                 剪枝/评估脚本（无在线服务）
├── frontend/                 Vue 3 SPA：src/{App.vue,api.ts,types.ts,main.ts,style.css}
│                             package.json  vite.config.ts  index.html  dist/  README.md
├── tmf_common/               网关与 worker 共享契约/配置（零重依赖）
│   ├── prediction.py         PredictService 模板 + PredictionResult / 错误类型
│   ├── flask_shell.py        worker 薄 Flask 入口（/predict_api、健康端点）
│   └── config.py             host/port/URL/CORS 环境变量解析与校验
├── serving/                  统一服务层
│   ├── adapters/             ModelAdapter 协议 + HttpWorkerAdapter + LlmAdapter
│   ├── config/               workers.yaml  models.yaml  processes.yaml
│   ├── gateway/              FastAPI 网关（main.py / routers/ / errors / settings / probes / stats）
│   ├── models/               registry.py（http_adapter.py 保留为旧名兼容层）
│   ├── services/             classification.py（只依赖 ModelAdapter 协议）
│   ├── scripts/              stage2.ps1  validate_config.py  verify_stage2.py
│   │                         verify_stage3.py  mock_worker.py
│   │                         verify_refactor_gateway.py  verify_refactor_workers.py
│   ├── logs/  run/           运行日志与状态（gitignore）
│   └── README.md             服务能力、适配层、配置与验收
├── tools/                    prepare_data.py  handoff_models.py
│                             clean_data.py    patch_fasttext_numpy2.py
├── docs/                     工程/指标/联调/前端/基线等 7 份文档
├── requirements.txt          ML 与 worker 依赖（3 条硬版本约束）
├── run_all.py                训练流水线骨架（按阶段调用各模块脚本）
├── 启动TMF.bat / 停止TMF.bat   Windows 一键启停
├── TODO-联通清单.md           待办与联通记录
└── README.md
```

> `PretrainedModel/`（6 个模型，约 9.27 GB）在**工程同级目录** `../PretrainedModel`，不在本工程内，也不入库。

---

## 五、快速开始

### 5.1 前置条件

| 项 | 要求 |
|---|---|
| OS | Windows 10/11（`stage2.ps1` / BAT 为 Windows 专属） |
| Python | 3.10 或 3.11（fastText 的 Windows 轮子只到 cp312；本机使用 `D:\Miniconda3\envs\py3109`） |
| 网关依赖 | 同级 `venv-gateway\Lib\site-packages`（`serving/requirements-gateway.txt`，由 `stage2.ps1` 注入 `PYTHONPATH`） |
| Node | 24 LTS + pnpm（前端 `node_modules` 已安装过则无需重复安装） |
| 模型产物 | `01-rf/save_model/rf.pkl`、`02-fasttext/save_model/word_auto_train.pkl`、`03-bert/save_model/bert.pkl`、`05-distill/save_model/student_v2b.pkl` 等（训练步骤见第七节） |
| 预训练模型 | `../PretrainedModel/bert-base-chinese`（可用 `TMF_BERT_PATH` 覆盖） |

```powershell
# ML / worker 依赖（注意 requirements.txt 的三条硬约束）
pip install -r requirements.txt

# 前端依赖（仅首次）
cd frontend
pnpm install
```

**三条必须遵守的版本约束（均已实测）：**

| 约束 | 原因 |
|---|---|
| `numpy<2` | fasttext 的 `FastText.py` 使用 `np.array(probs, copy=False)`，NumPy 2.x 抛 `ValueError` |
| `transformers<5` | 5.x 移除 `tokenizer.batch_encode_plus`，本工程多处以它做预处理/推理 |
| 用 `fasttext-wheel` 而非 `fasttext` | `pip install fasttext` 在 Windows 无预编译轮子，需 MSVC 编译 |

> 本机 `py3109` 的 numpy 实际为 2.2.6（被其它库锁定），已对 fasttext 打补丁；**全新环境仍建议按 `numpy<2` 安装**。若不得不用 numpy 2.x，执行一次 `python tools/patch_fasttext_numpy2.py`（幂等、自动备份）。

### 5.2 一键启动 / 停止

```powershell
# 双击或在项目根目录执行
启动TMF.bat     # 先等后端全部 ready，再启动前端
停止TMF.bat     # 停止网关 + 5 个 worker + 前端窗口
```

- 浏览器打开 **`http://127.0.0.1:5173`**（本地默认）；局域网访问见 5.5。
- `启动TMF.bat` 是幂等的：后端已在运行会复用；前端已在运行（探测到 TMF 页面）会跳过，不会报端口占用。
- 若其他程序占用前端端口，脚本会明确报错；先关闭占用进程或运行停止脚本。
- 局域网访问需要按 5.5 显式配置监听地址，并在 Windows 防火墙放行对应端口。

### 5.3 手动分步启动

```powershell
# 后端：网关 + 5 个 worker（校验配置、检查模型产物、等待就绪）
powershell -NoProfile -ExecutionPolicy Bypass -File serving\scripts\stage2.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File serving\scripts\stage2.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File serving\scripts\stage2.ps1 -Action stop

# 前端（另一个窗口）
cd frontend
pnpm dev
```

### 5.4 端口与地址

| 组件 | 地址 | 说明 |
|---|---|---|
| Vue 前端（Vite） | `http://127.0.0.1:5173` | `strictPort: true`，端口占用直接失败 |
| FastAPI 网关 | `http://127.0.0.1:8000` | 浏览器与前端唯一入口 |
| RF worker | `http://127.0.0.1:8101` | 随机森林 |
| fastText worker | `http://127.0.0.1:8103` | 8102 未使用 |
| BERT worker | `http://127.0.0.1:8104` | fp32 `bert.pkl` |
| LLM worker | `http://127.0.0.1:8105` | 调用外部 Provider，可能计费 |
| 蒸馏 worker | `http://127.0.0.1:8106` | v2b 学生模型 |
| 剪枝 | — | 无在线服务，仅离线脚本 |

> 地址来源：`serving/config/processes.yaml`（网关端口）、`serving/config/workers.yaml`（worker 调用 URL）、`tmf_common/config.py`（环境变量默认/校验）、`frontend/vite.config.ts`（前端监听）、`frontend/src/api/`（API 基址）。换机器/换 IP 时**不需要改源码**，按 5.5 用环境变量覆盖即可。

### 5.5 环境配置（本地 / 局域网）

所有可配置项都有本地默认值；局域网通过环境变量覆盖，**不要把某台机器的 IP 写进源码或 YAML**。

| 变量 | 作用 | 默认 |
|---|---|---|
| `TMF_BIND_HOST` | 5 个 Flask worker 的监听地址 | `127.0.0.1` |
| `TMF_GATEWAY_HOST` / `TMF_GATEWAY_PORT` | 网关监听地址 / 端口 | `127.0.0.1` / `8000` |
| `TMF_FRONTEND_HOST` / `TMF_FRONTEND_PORT` | Vite 监听地址 / 端口 | `127.0.0.1` / `5173` |
| `TMF_WORKER_HOST` | 网关调用 worker 时使用的主机（端口沿用 YAML） | 取 YAML 中的值（`127.0.0.1`） |
| `TMF_WORKER_URL_<ID>` | 单个 worker 的完整 URL 覆盖（如 `TMF_WORKER_URL_RF`） | 未设置 |
| `TMF_WORKER_PORT_<ID>` | 手动启动单个 worker 时的端口覆盖（如 `TMF_WORKER_PORT_RF`） | 各模块默认端口 |
| `TMF_CORS_ORIGINS` | 追加允许的 CORS 来源（逗号分隔，禁止 `*`） | 默认只含 `http://<frontend-host>:<port>` |
| `VITE_API_BASE_URL` | 前端访问网关的基址（`.env.local` 可覆盖） | `http://127.0.0.1:8000` |

**局域网示例**（PowerShell，启动前设置；`<LAN_IP>` 换成你的网卡地址）：

```powershell
$env:TMF_BIND_HOST='0.0.0.0'                 # worker 监听所有网卡
$env:TMF_GATEWAY_HOST='0.0.0.0'              # 网关监听所有网卡
$env:TMF_FRONTEND_HOST='0.0.0.0'             # Vite 监听所有网卡
$env:TMF_WORKER_HOST='<LAN_IP>'              # 网关调用 worker 的目标主机
$env:VITE_API_BASE_URL='http://<LAN_IP>:8000'
$env:TMF_CORS_ORIGINS='http://<LAN_IP>:5173'
```

- **监听地址 ≠ 客户端访问地址**：`0.0.0.0` 只能用于监听，浏览器/网关不能把它当连接目标。
- CORS 只放行显式来源；LAN 场景必须追加 `TMF_CORS_ORIGINS`（或把 `TMF_FRONTEND_HOST` 设为具体网卡 IP）。
- 前端也可在 `frontend/.env.local` 写 `VITE_API_BASE_URL`（该文件不入库）。

---

## 六、数据与模型资产

- **数据**：`00-data/data/train.txt`（180,000）、`dev.txt`（10,000）、`test.txt`（10,000），格式 `标题\t标签`；`class.txt` 为 10 类唯一来源（行序即类别 ID）。
- **数据同步**：各模块的 `data/` 由 `python tools/prepare_data.py` 从 `00-data/data` 生成（不入库）。
- **清洗集（实验）**：`train.clean.txt`（178,692，-0.73%），报告 `00-data/data/clean_report.md`；仅蒸馏 v2b 使用，不作为主口径。
- **预训练模型**：`../PretrainedModel/bert-base-chinese`（另有 5 个参考模型，本项目只用 bert-base-chinese）。
- **产物规模**：主口径模型 + 断点约 **6.7 GB**（RF 1.5 GB、fastText 2.49 GB、BERT 390 MB、断点 1.17 GB、量化 153 MB、蒸馏/剪枝若干）；若统计 5 个 `save_model/` 全部文件（含 `_clean` 变体与 `checkpoint.pt`），2026-10-20 实测合计 **10,864,626,128 B ≈ 10.86 GB**（见 [docs/阶段0-工程基线报告.md](docs/阶段0-工程基线报告.md) §3.6）。清洗实验产物约 3.7 GB；`PretrainedModel` 9.27 GB。上述均在 `.gitignore` 中排除。

---

## 七、训练与复现

### 7.1 一键流水线（骨架）

```powershell
python run_all.py --list        # 查看阶段
python run_all.py --stage rf    # 只跑到指定阶段
python run_all.py               # 按序调用各模块脚本
```

阶段：`prepare → rf → rf-train → ft-pre → ft-train → bert-train → bert-quant → handoff → distill → prune`。
注意：`run_all.py` 里的 distill 使用 **v1** `model_distll.py`；v2 / v2b 需单独运行（见 7.2）。该脚本是「按顺序调用模块脚本」的骨架，未包含服务启动。

### 7.2 分模块训练（实测耗时）

| 步骤 | 命令（模块目录内） | 实测耗时 | 主要产物 |
|---|---|---|---|
| RF 预处理 | `python data_preprocessing.py` | 23.4 s | `01-rf/data/process_*.csv` |
| RF 训练 | `python rf_train.py` | 628.3 s | `rf.pkl`（1,512 MB）、`tfidf.pkl` |
| fastText 预处理 | `python data_preprocessing.py` | 31.8 s | 字符级/词级数据 |
| fastText 训练 | `python ft_train.py` | 528.0 s | 4 个模型，合计 2.49 GB |
| BERT 训练（B 方案） | `python bert_train_and_eval.py` | 5 h 20 m（1 epoch，CPU） | `bert.pkl`（390.2 MB）、`checkpoint.pt`（1,166 MB） |
| 动态量化 | `python 模型量化_DQ.py` | 545.7 s | `quantization_model.pkl`（152.7 MB） |
| 模型交接 | `python tools/handoff_models.py` | 秒级 | `teacher_bert.pkl`、`before_prune_model.pkl` |
| 蒸馏 v1 | `python model_distll.py` | 2 h 13 m | `student_bert.pkl`（26.6 MB） |
| 蒸馏 v2/v2b | `python distill_v2.py` | v2 2 h 22 m + 教师缓存约 2 h；v2b 续训 40 m | `student_v2.pkl` / `student_v2b.pkl`（36.7 MB） |
| 剪枝 | `python model_prune.py` | 705.3 s | `before/after_prune_model.pkl`（各 390.2 MB） |

> 所有 `config.py` 用 `__file__` 推绝对路径，**在哪个目录运行都可以**；小样本冒烟可用 `TMF_TRAIN_PATH` / `TMF_DEV_PATH`。

### 7.3 实测指标（独立 test 集 1 万条，主口径）

| 模型 | 体积 | macro-F1 | 1 万条推理 | 定位 |
|---|---|---|---|---|
| 随机森林（TF-IDF） | 1,512 MB | 0.8302 | 1.2 s | 基线 / 最低延迟 |
| fastText 词级 auto | 451 MB | 0.9173 | 秒级 | 词向量基线 |
| **BERT 全量微调 fp32** | 390.2 MB | **0.9307** | 339 s | 精度最高 |
| BERT 量化 int8（DQ） | **152.7 MB** | 0.9263 | **120 s** | 性价比最高 |
| BERT 剪枝 30% | 390.2 MB | 0.9294 | 336 s | 精度几乎无损，体积不变 |
| 蒸馏 BiLSTM v2b | **36.7 MB** | 0.9074 | **13 s** | 最轻量可用（清洗集，唯一例外） |

详细轨迹、清洗实验与口径说明见 [docs/实测指标.md](docs/实测指标.md)。

---

## 八、服务接口契约

### 8.1 统一网关（前端唯一入口）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/healthz` | 网关进程存活 |
| GET | `/api/v1/readyz` | 汇总 5 个 worker 的就绪状态 |
| GET | `/api/v1/models` | 模型清单（默认模型、类别、启用状态、版本） |
| GET | `/api/v1/stats` | 单进程内存统计（重启清零） |
| POST | `/api/v1/classify` | 单模型分类：`{"title": "...", "model": "bert"}`（`model` 可选，默认 fasttext） |
| POST | `/api/v1/compare` | 多模型对比：`{"title": "...", "models": ["rf","fasttext","bert","distill"]}`（可省略） |

- 标题上限 200 字符（`TMF_MAX_TITLE_CHARS`）；类别白名单唯一来源 `00-data/data/class.txt`。
- 请求编号：`X-Request-ID`（`^[A-Za-z0-9._-]{1,64}$`），网关生成/透传并在响应头回显。
- 错误统一为 `{"request_id": "...", "error": {"code": "...", "message": "...", "details": {...}}}`。
- CORS 默认只允许 `http://<TMF_FRONTEND_HOST>:<TMF_FRONTEND_PORT>`（默认 `http://127.0.0.1:5173`）；`TMF_CORS_ORIGINS` 可追加明确来源（禁止 `*`）。
- `/compare` 允许部分模型失败：返回 `summary{total,ok,failed,status}` + 逐模型 `results`。

### 8.2 模型 worker（内部契约）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/predict_api` | `{"title": "..."}` → `{"title": "...", "pred_class": "sports"}` |
| GET | `/healthz` | 进程存活 |
| GET | `/readyz` | 模型已加载（LLM 仅表示本地服务与客户端已初始化，**不验证密钥**） |
| GET | `/metadata` | 稳定 `model_id` / 展示名（版本当前为 `null`） |

- 每个入口 `api_flask_server.py` 是薄壳；五个模型共用 `tmf_common/flask_shell.py` 的路由与 `tmf_common/prediction.PredictService` 的「标题校验 → 推理 → 白名单校验 → 结果」流程。
- 各模型仍保留自己的预处理（jieba / TF-IDF / BERT tokenizer / BiLSTM / LLM Prompt），不强行合并。

### 8.4 模型适配层（网关内部）

| 类型 | 作用 |
|---|---|
| `ModelAdapter`（`serving/adapters/protocol.py`） | 网关业务层唯一依赖的协议：`predict(title, request_id) -> AdapterResult`（含 `pred_class` / `latency_ms` / 错误码） |
| `HttpWorkerAdapter` | 通过 HTTP 调用某个 Flask worker；超时/非 200/非法 JSON/缺字段/非法类别归一化 |
| `LlmAdapter` | LLM worker 适配器（仍是本地 HTTP；Provider 密钥只在 04-LLM 进程，网关不读取） |
| 装配位置 | `serving/gateway/main.py` 的 lifespan（composition root）；`ClassificationService` 只接收 `Mapping[str, ModelAdapter]` |

### 8.3 大模型服务

- Provider 由 `TMF_LLM_PROVIDER=deepseek|qwen|zhipu` 切换，密钥只从 `04-LLM/.env` 读取（已 gitignore）。
- 总时间预算 12 s（小于网关对 8105 的 15 s 超时），SDK 自动重试关闭；输出做 10 类白名单校验 + 至多一次重试。
- 前端默认不勾选大模型；真实调用可能产生费用。集成测试使用 `serving/scripts/mock_worker.py` 或 `04-LLM/verify_llm_timeout.py`（本地 mock 上游），**不访问真实 Provider**。

---

## 九、验证与测试

```powershell
# 配置 / 适配层 / worker 契约（不加载模型、不启动服务）
$env:PYTHONPATH='E:\AIModels\test\TouManFen\venv-gateway\Lib\site-packages'
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_refactor_gateway.py
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_refactor_workers.py

# 端到端验收（先 start；真实 worker）
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_stage2.py
D:\Miniconda3\envs\py3109\python.exe serving\scripts\verify_stage3.py --llm-mode skip

# 前端类型检查 + 生产构建
cd frontend
pnpm type-check
pnpm build
```

- `verify_refactor_gateway.py`：配置默认/覆盖/非法拒绝、`HttpWorkerAdapter`/`LlmAdapter` 全错误路径、fake adapter 注入、网关 6 个 API、request ID、CORS、运行时代码无硬编码 IP（2026-10-21 本机 **RESULT PASS**）。
- `verify_refactor_workers.py`：`PredictService` 模板、历史兼容 JSON、Flask 薄壳端点、未就绪 503、5 个 worker 入口静态检查（2026-10-21 本机 **RESULT PASS**）。
- `verify_stage2.py`：网关只读端点 + 5 个 worker 健康端点 + 4 个本地模型旧 `/predict_api` 契约（**35 项**；2026-10-21 重构后真实服务 **RESULT PASS**）。
- `verify_stage3.py`：分类/对比/边界/错误码；2026-10-21 真实服务 `--llm-mode skip` **RESULT PASS**；`--llm-mode mock` 在 5 个 mock worker 栈上 **RESULT PASS**（含 LLM 错误/超时映射，不访问真实 Provider）。
- 前端：2026-10-21 `pnpm type-check` 与 `pnpm build` 通过（`dist/` 已重建）。
- 已知边界：未做真实浏览器人工视觉验收；`compare` 的界面点击路径未验收（接口层已验证）。

---

## 十、配置项

| 配置 | 作用 | 位置/默认 |
|---|---|---|
| `TMF_BERT_PATH` | 预训练 BERT 目录 | 默认 `../PretrainedModel/bert-base-chinese` |
| `TMF_FREEZE_BERT` | 是否冻结编码器 | `0`=全量微调（当前默认/已跑），`1`=只训线性头 |
| `TMF_EPOCHS` / `TMF_LR` / `TMF_RESUME` | 训练轮数 / 学习率 / 断点续训 | 见 `03-bert/config.py` |
| `TMF_TRAIN_PATH` / `TMF_DEV_PATH` | 小样本冒烟数据路径 | 见 `03-bert/config.py` |
| `TMF_DEVICE` / `TMF_BATCH_SIZE` / `TMF_MAX_LENGTH` / `TMF_TEST_PATH` | 设备（默认 `cpu`）/ batch / 序列长度 / 测试集路径 | 见 `03-bert/config.py` |
| `TMF_EXPERIMENT_TAG` | 产物加后缀，隔离实验（如 `_clean`） | 各模块 `config.py` |
| `TMF_USE_CLEAN` / `TMF_KEEP_IDX` / `TMF_INIT_STUDENT` | 蒸馏清洗集 / 清洗索引 / 学生初始化 | `05-distill/` |
| `TMF_LLM_PROVIDER` | `deepseek` / `qwen` / `zhipu` | 默认 `deepseek` |
| `TMF_LLM_TOTAL_TIMEOUT_S` | LLM 上游总预算（默认 12 s） | `04-LLM/llm_predict_service.py` |
| `TMF_BIND_HOST` | 5 个 worker 监听地址 | 默认 `127.0.0.1`（`tmf_common/config.py`） |
| `TMF_GATEWAY_HOST` / `TMF_GATEWAY_PORT` | 网关监听地址/端口 | 默认 `127.0.0.1` / `8000` |
| `TMF_FRONTEND_HOST` / `TMF_FRONTEND_PORT` | 前端监听地址/端口 | 默认 `127.0.0.1` / `5173` |
| `TMF_WORKER_HOST` / `TMF_WORKER_URL_<ID>` | 网关调用 worker 的主机/完整 URL 覆盖 | 默认取 `workers.yaml` |
| `TMF_CORS_ORIGINS` | 追加 CORS 来源（逗号分隔，禁止 `*`） | 默认由前端地址推导（`tmf_common/config.py`） |
| `TMF_DEFAULT_MODEL` / `TMF_MAX_TITLE_CHARS` | 默认模型 / 标题上限 | 覆盖 `serving/config/models.yaml` |
| `TMF_WORKERS_FILE` / `TMF_MODELS_FILE` / `TMF_CLASS_FILE` | 覆盖配置文件路径 | 见 `serving/gateway/settings.py` |
| `TMF_PROBE_TIMEOUT_MS` / `TMF_STATUS_CACHE_MS` | worker 探测超时 / 状态缓存 | 覆盖 `workers.yaml` |
| `VITE_API_BASE_URL` | 前端 API 基址 | 默认 `http://127.0.0.1:8000`（`.env.example`） |

---

## 十一、关键约束与设计决策

1. **前端只访问网关**：worker 地址、超时、错误归一化集中在网关；网关不 import torch/transformers。
2. **统一契约**：网关 `/api/v1/*` 返回 `request_id`；worker 统一 `POST /predict_api` + `title` / `pred_class`；worker 内部统一 `PredictService` 模板。
3. **配置集中在 `serving/config/*.yaml` + 环境变量**：类别唯一来源 `class.txt`；启动前 `validate_config.py` 校验（端口一致性、敏感字段禁止、重复键拒绝、网络变量合法性）。
4. **地址可配置，不写死网卡**：默认 `127.0.0.1`；局域网通过 `TMF_BIND_HOST` / `TMF_GATEWAY_HOST` / `TMF_FRONTEND_HOST` / `TMF_WORKER_HOST` / `TMF_CORS_ORIGINS` 覆盖（见 5.5），源码、YAML、BAT/PS1 中不再有固定 IP。
5. **CPU-only**：BERT 全量微调 1 epoch 约 5.3 小时；训练产物/断点均在本地，不随仓库分发。
6. **版本锁定**：`numpy<2`、`transformers<5`、`fasttext-wheel` 三条硬约束；`scikit-learn==1.7.*` 与 Python 3.10 组合。
7. **网关启动方式**：本机 `venv-gateway` 的 Python 存在 `_ssl` DLL 加载故障，`stage2.ps1` 用 `py3109` 执行网关并从 `venv-gateway\Lib\site-packages` 注入依赖；worker 仍用 `py3109`。
8. **Windows 启停脚本**：BAT 使用纯 ASCII + CRLF；前端启动带 5173 探测（幂等）；停止脚本按 PID/命令行归属校验，不按镜像名批量杀进程。
9. **局域网 HTTP 不是安全上下文**：`crypto.randomUUID`、`crypto.subtle`、`navigator.clipboard` 等 `[SecureContext]` API 不可用；前端 `requestId()` 已实现「`randomUUID` → `getRandomValues` → 时间戳」三级回退。
10. **数据口径**：主口径为未清洗数据；清洗集重训与蒸馏 v2b 属附加实验，结论单独标注。

---

## 十二、已知限制与未完成

- **非生产**：无鉴权、无 HTTPS、无用户体系、无生产级数据库/迁移（仅本地 SQLite 存反馈/投票）、无缓存/队列/批量分类、无 Docker/CI；网关与 worker 均为开发服务器单进程。
- **LLM 未系统评估**：仅有样例调用与 16 次网关成功调用（2026-10-20 16:22–18:06，0.5–4.0 s，见 `serving/logs/gateway.jsonl`）；准确性、费用、稳定性未做统计评估；`/readyz` 不代表供应商当前可用。
- **热搜业务未实现**：按用户要求延期（见 `TODO-联通清单.md`）。
- **剪枝不压缩体积**：非结构化剪枝 + `prune.remove()` 固化后仍是稠密存储，只提升稀疏度。
- **量化模型未上服务**：`quantization_model.pkl` 仅离线评估，在线集合仍是 fp32 BERT。
- **默认仅监听回环地址**：本地单机开箱可用；局域网必须按 5.5 设置 `TMF_BIND_HOST` / `TMF_GATEWAY_HOST` / `TMF_FRONTEND_HOST` / `TMF_CORS_ORIGINS`，否则同网段无法访问。
- **仓库状态**：工程目录已有 `.git`（2026-10-21 初始化并推送，HEAD `9f81e05`，见 [v4 Git 归档](docs/PROJECT_LEARNING_GUIDE_v4_Git与GitHub工程归档.md)）；本次重构改动**尚未提交**，无法用提交历史复核这些改动；提交前需确认 `.gitignore` 已排除模型/密钥/数据副本。
- **前端未做真实浏览器视觉验收**：仅接口层、无头浏览器冒烟与构建验证；桌面/窄屏观感、交互细节需人工检查；`compare` 的界面点击路径同样未做浏览器验收。
- **停止脚本**：后端 stop 已于 2026-10-20 18:17 经 `serving/logs/stage2-script.log` 确认"全部已停止，状态文件已清理"；前端窗口关闭按窗口标题匹配，仍建议在普通桌面会话复测一次。

---

## 十三、常见问题排查

1. **5173 打不开 / 端口被占**：重新执行 `启动TMF.bat` 会自动跳过已在运行的前端；若被其他程序占用，脚本会报错，先关闭占用进程或运行 `停止TMF.bat`。
2. **页面提示“暂时无法连接 TMF 后端”**：① 先确认网关：`curl http://127.0.0.1:8000/api/v1/healthz`（LAN 用实际网卡 IP）；② 打开浏览器控制台，若有 `crypto.randomUUID is not a function` 之类 `TypeError`，说明是非安全上下文问题（已修复，需强制刷新页面）；③ 确认页面与 API 使用同一个主机（LAN 场景不要混用 `127.0.0.1`）；④ 确认 CORS 来源与 `TMF_CORS_ORIGINS`。
3. **网关连不上 worker / `readyz` 有 down**：查看 `serving/logs/gateway.err.log` 与 `serving/logs/workers/*.err.log`；确认 worker 端口与 `workers.yaml` 一致；`stage2.ps1 -Action status` 查看归属与状态。
4. **网关启动不 ready（`_ssl`）**：确认使用 `py3109` 且 `venv-gateway\Lib\site-packages` 存在；查看 `serving/logs/gateway.err.log`；不要用 `taskkill /IM python.exe` 清场。
5. **fastText 预测报 `Unable to avoid copy...`**：numpy 2.x 兼容问题，执行 `python tools/patch_fasttext_numpy2.py` 或安装 `numpy<2`。
6. **BERT 报 `batch_encode_plus` 不存在**：`transformers` 装到了 5.x，降到 `<5`。
7. **`pip install fasttext` 失败**：改用 `fasttext-wheel`。
8. **LLM 返回 401 / 超时**：检查 `04-LLM/.env` 的 `DEEPSEEK_API_KEY`（或对应 Provider）；联调优先用 mock。
9. **局域网其他成员打不开**：按 5.5 设置监听地址与 CORS，Windows 防火墙放行 5173/8000/8101/8103~8106 入站；确认同网段且客户端使用实际网卡 IP（不要用 `0.0.0.0` 访问）。
10. **训练很慢**：CPU-only 正常；BERT 支持 `TMF_RESUME` 断点续训，日志在 `03-bert/log/`。

---

## 十四、文档索引

| 文档 | 内容 |
|---|---|
| [serving/README.md](serving/README.md) | 服务层能力、启停、验收、边界 |
| [frontend/README.md](frontend/README.md) | 前端启动、LLM 边界、局域网 HTTP 约束 |
| [docs/实测指标.md](docs/实测指标.md) | 主口径对比、清洗实验、方法学说明 |
| [docs/联调记录.md](docs/联调记录.md) | 5 个模块接口/前端联调记录 |
| [docs/前端对接说明.md](docs/前端对接说明.md) | API 契约、CORS、启停、LLM 边界 |
| [docs/PROJECT_LEARNING_GUIDE.md](docs/PROJECT_LEARNING_GUIDE.md) | v1：早期 Streamlit/模型全流程学习档案（历史） |
| [docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md](docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md) | v2：服务层工程化与集成验证 |
| [docs/PROJECT_LEARNING_GUIDE_v3_前端开发全过程技术归档.md](docs/PROJECT_LEARNING_GUIDE_v3_前端开发全过程技术归档.md) | v3：前端开发全过程技术归档（历史记录，IP 为旧默认） |
| [docs/PROJECT_LEARNING_GUIDE_v4_Git与GitHub工程归档.md](docs/PROJECT_LEARNING_GUIDE_v4_Git与GitHub工程归档.md) | v4：Git/GitHub 工程归档 |
| [docs/PROJECT_LEARNING_GUIDE_v5_模型适配层与网络配置重构.md](docs/PROJECT_LEARNING_GUIDE_v5_模型适配层与网络配置重构.md) | v5：适配层、worker 契约与网络配置重构（前期归档） |
| [docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md](docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md) | v6：D5/D6 全链路、离线基准、演示真实性、F15 启动来源、D4 隔离复验与工程复盘 |
| [docs/阶段0-工程基线报告.md](docs/阶段0-工程基线报告.md) | 环境、依赖、模型基线 |
| [TODO-联通清单.md](TODO-联通清单.md) | 待办与历史联通记录（2026-10-21 已按当前状态回填；历史段落保留） |

工作区其他输入资料（与本工程同级）：

- `../参考代码/gzai6_tmf/`：原始课程参考代码（改造来源）。
- `../PretrainedModel/`：6 个预训练模型（本项目只用 `bert-base-chinese`）。
- `../参考资料/`：课程 PPT 与 7 篇笔记。
- `../V3.0.md`、`../V3.0-实测验证报告.md`、`../V3.0-可行性检查报告.md`、`../V3.0-项目可行性重估报告.md`、`../工程实施工作清单.md`：需求/可行性/实施规划资料。

---

## 十五、变更记录（近期）

| 日期 | 变更 |
|---|---|
| 2026-10-16 | 全链路训练与评估完成：RF / fastText ×4 / BERT 全量微调 / 量化 / 剪枝 / 蒸馏 v1-v2b；指标回填 `docs/实测指标.md` |
| 2026-10-19 | 5 个模块接口与 Streamlit 前端联调通过；LLM 切换 DeepSeek；fastText × numpy 2 补丁；端口分离与缺参兜底修复 |
| 2026-10-20 | 新增 FastAPI 统一网关 + Vue 3 前端（单模型/对比）；服务地址整体切换到 `192.168.12.54`（局域网访问）；启动 BAT 增加 5173 幂等探测；修复局域网 HTTP 非安全上下文下 `crypto.randomUUID` 导致的“无法连接后端”；README 系统性重写 |
| 2026-10-20（晚） | 状态文档核对：回填 `TODO-联通清单.md`、`serving/README.md`、本 README；为阶段 0 报告、联调记录、两份学习档案、工程实施清单加历史基线/状态补记；前端归档与前端对接说明补充构建指纹、`/compare` 网关日志与 stop 验证证据；按网关日志修正"LLM 仅 mock、未经网关验证"的口径（实有 16 次真实成功调用）。**未改代码/数据/配置/密钥，未初始化 Git。** |
| 2026-10-21 | **架构重构**：新增 `serving/adapters/`（`ModelAdapter` 协议 + `HttpWorkerAdapter` / `LlmAdapter`，网关只依赖协议）；新增 `tmf_common/`（`PredictService` 模板 + 薄 Flask 入口 + 网络配置解析）；5 个模型预测服务统一为 `SERVICE + predict(dict)` 兼容层，`api_flask_server.py` 变为薄壳；移除运行路径中的固定 IP，默认 `127.0.0.1` 并支持环境变量/LAN 覆盖。验证：`verify_refactor_gateway.py` / `verify_refactor_workers.py` / `verify_stage2.py`（真实 35 项）/ `verify_stage3.py --llm-mode skip`（真实）/ `--llm-mode mock`（mock 栈）全部 PASS；前端 type-check + build 通过。 |
| 2026-10-21（下午） | 学习档案版本重排：前端归档 → `v3`，Git 归档 → `v4`，适配层重构 → `v5`；同步 README 索引与所有文档内链接；更新当前运行状态（LAN 模式）。 |

---

## 十六、安全提示

- `04-LLM/.env` 含真实 API Key，已 gitignore；本工程源码中不再有明文密钥。历史 Key 曾暴露，建议到各平台重置。
- **外部资料仍有历史密钥**：`../参考资料/03-笔记/05_LLM实现.md` 与 `../参考代码/gzai6_tmf/04-LLM/`（含 `.env` 与平台示例脚本）不属本工程源码，其中仍含课程阶段的明文 key（联调记录显示旧 key 曾 401，仍按敏感信息处理）；本次未修改这些资料，对外分发/上传前需清理或脱敏。
- **代码与第三方材料的所有权区分**：项目代码归项目所有者所有，并已由所有者确认可上传；`PretrainedModel/`、`数据/`、`参考资料/` 等数据集、预训练模型与课程资料的来源与许可**未在本工程内核对**，分发/对外发布前需单独确认，本工程不作授权声明。
- 服务默认只监听 `127.0.0.1` 且**无鉴权**：按 5.5 改为 LAN 监听后，同网段任何人都能调用 5 个模型（含 LLM 接口，会产生真实费用）。若只需本机使用，保持默认；若只需暴露前端/网关，可只让网关监听局域网、worker 留在回环地址。
- 代码与模型数据均在本机，训练产物（主口径约 6.7 GB；5 个 `save_model/` 全部文件实测约 10.86 GB）、预训练模型（9.27 GB）、`.env` 均不入库；首次 `git init` 提交前用 `git status` 复查。

---

> 本 README 于 2026-10-20 按当前工程状态系统性重写。指标与耗时均来自本机实测；环境/版本差异可能导致结果不同，以 `docs/实测指标.md` 与各模块日志为准。


---

## 十七、最终收尾状态（D1，2026-10-22）

> 本节描述截至 2026-10-22 的实现与验证状态；历史章节保留原文，不追改历史归档。

### 17.1 已实现能力

- 分类/对比：`/classify`、`/compare` 保留；新增 `GET /compare/stream` SSE（逐模型、15s 心跳、总截止 18000ms、断连清理），`POST /compare` 行为不变。
- 分数与拒识：`scores/score_kind/top_k`、`rejected/reject_reason/threshold_meta`；软拒识保留原预测；阈值来自 B3 合成负样本 ROC（0.518/0.699，未校准，适用范围有限）。
- 反馈：`POST /feedback`、`GET /feedback/stats`；pending、去重、30 天清理、相似 hint；无审核后台/训练。
- 竞技场：匿名抽题/投票/leaderboard（Elo + Wilson，n<50 预热）；人类偏好，不是准确率。
- 离线基准：`GET /bench/pk` 读取 `artifacts/bench/pk_2026-10-22.json`；4 个本地模型（RF/fastText/BERT/distill）已评估，**LLM 为 missing（外部 Provider 未授权）**；集成/oracle 为派生指标。
- 演示真实性：worker `/metadata.display_name` 判定 live/mock（`mock-` 前缀=mock），无法判定为 unknown；`data_source` 透传 classify/compare/SSE；统计只计 live。
- 快照/演示数据：`artifacts/demo/snapshot_2026-10-22.json`、`frontend/public/snapshot.json`（8 条 cases，cached，LLM missing）；`serving/scripts/demo_seed.py --reset` 使用显式临时 `TMF_DB_PATH`。
- 前端：暗色/亮色/大屏、三档断点、F10 竞技场页、F11 PK 榜、F14 可访问性/焦点/reduced-motion；`#arena`/`#bench` 懒加载，默认首页不触发竞技场抽题。

### 17.2 关键接口

`GET /api/v1/bench/pk`、`GET /api/v1/compare/stream`、`POST/GET /api/v1/feedback*`、`/api/v1/arena/next|vote|leaderboard`。字段与错误码见 [接口变更记录](docs/接口变更记录_2026-10-21.md) 与 [前端对接说明](docs/前端对接说明.md)。

### 17.3 验证结果快照（2026-10-22）

- B9：`verify_b9_llm_optin.py` **16 PASS**；默认 compare 不含 LLM、mock 调用计数 0；显式 LLM 离线 → `partial`。
- B11：`verify_b11_data_source.py` **39 PASS**；mock/live/unknown 判定、统计只计 live、快照 meta 校验通过。
- B10：`verify_refactor_gateway.py` **101 PASS**、`verify_refactor_workers.py` **93 PASS**、`verify_stage3.py --llm-mode mock --isolate-mock` **59 PASS**（隔离临时端口/DB，已清理）。
- D2：8 条演示标题、20 道 arena 演示题、`frontend/public/snapshot.json`（8 cases、cached、LLM missing）；`demo_seed.py --reset` 两次临时 DB 幂等。
- F14：6 场景/66 断言（skip link、键盘、焦点、reduced-motion、6× CPU、50 卡）PASS；`vue-tsc`/`pnpm build` 通过。
- D4：**完整 10 步正常彩排 PASS**（隔离网关 14520 + mock worker + 临时 DB + envDir 隔离构建，10/10，7.584s）；5 个故障场景均有隔离证据：LLM 离线 partial→恢复、后端全停→cached→恢复、地址不可达 cached、mock 模式独立 10 步、并发 2/总截止 25s 实际 UI。全场景网络审计 `bad_network=[]`。详见 `docs/彩排记录_2026-10-22.md`。

### 17.4 验证命令（本机、隔离环境）

```powershell
$env:PYTHONPATH='E:\AIModels\test\TouManFen\venv-gateway\Lib\site-packages'
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_refactor_gateway.py
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_refactor_workers.py
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_stage3.py --llm-mode mock --isolate-mock
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_b9_llm_optin.py
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_b11_data_source.py
D:\Miniconda3\envs\py3109\python.exe serving/scripts/verify_f15_startup.py
```

> 上述后两项为 B9/B11 的隔离验收脚本；**未调用真实 LLM/Provider**。真实 stage3 与用户 stage2 未在本轮操作。

### 17.5 当前限制（必须如实说明）

- **B8 LLM 缺失**：LLM 未纳入离线基准；PK 榜显示 missing，不得用 mock/估算分数补齐。
- **F15 启动来源与 cached 回退已接线并验证**：页面加载即请求 `GET /api/v1/demo/state`，由服务端 metadata 判定 mock/live；全 ready 且来源一致才显示 live/mock，混合/缺失/不可达为 unknown 并展示来源明细；后端不可达时自动读取 `public/snapshot.json`，显示“离线快照”+生成时间+8 条 cached 内容，恢复后退出；快照缺失/非法显示错误。分类/对比结果仍按各自 `data_source` 更新来源。
- **F13 设备验收为模拟**：Edge headless/CDP 模拟 1920×1080、1280×720、390×844、125%；没有真实投影/手机/笔记本验收。
- 未做真实 LLM 调用、未训练、未对用户 stage2 压测；B12 真实模型并发 1/3/5 为隔离本地测量。
- D4 彩排使用隔离 mock/fixture，不等同真实生产/真实外部 Provider 验收。
- 文案红线：反馈只能“已提交/已记录，待审核”；Elo 只能“人类偏好，仅供演示参考”；离线基准不得称“实时准确率”；mock/cached 不得标 live。
