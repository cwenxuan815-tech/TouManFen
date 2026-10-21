# 投满分新闻分类项目（TouManFenClassifier）

把中文新闻标题自动分到 10 个频道（`finance, realty, stocks, education, science, society, politics, sports, game, entertainment`），覆盖一条完整链路：

> 数据准备 → 随机森林基线 → fastText → BERT 微调 → 大模型（DeepSeek）→ 模型压缩（量化 / 蒸馏 / 剪枝）→ 统一服务层 → Vue 前端

项目已从「课程参考代码」整理为一个可训练、可评估、可服务、可局域网访问的验证型工程。**当前定位是本地/局域网验证环境，不是生产系统**（无鉴权、无 HTTPS、无数据库、无容器化，详见「已知限制」）。

> **文档状态（2026-10-20 晚）**：已按当前代码、配置与验证记录核对能力状态；文中"已实现"与"已验证"分别标注，历史验证均注明日期与来源。本次只更新文档，未改代码/数据/配置/密钥，未初始化 Git。

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
| 5 个 Flask 模型服务 | ✅ | 8101/8103/8104/8105/8106，统一 `POST /predict_api` |
| FastAPI 统一网关 | ✅ | 8000；模型清单、健康探测、单模型分类、多模型对比、统计 |
| Vue 3 单页前端 | ✅ 已实现；接口层已验证 | 5173；单模型 / 多模型对比、LLM opt-in、错误与请求编号；未做真实浏览器人工视觉验收 |
| 局域网访问 | ✅ | 网关、worker、前端统一监听 `192.168.12.54` |
| 一键启停（Windows） | ✅ | `启动TMF.bat` / `停止TMF.bat`，可重复执行；后端 stop 于 2026-10-20 18:17 经日志确认 |
| 自动化验收脚本 | ✅ 已实现；已验证（历史） | `verify_stage2.py`（35 项）、`verify_stage3.py`（skip/mock 两模式）；2026-10-20 记录 RESULT PASS，**本次文档核对未复跑** |

**主口径**：除蒸馏 v2b（清洗集训练，唯一例外）外，服务端模型均由**未清洗的 180,000 条**训练；量化 / 剪枝由 BERT 派生。指标为独立 `test.txt`（1 万条）实测，详见 [docs/实测指标.md](docs/实测指标.md)。

---

## 二、技术栈

| 层 | 技术选型 |
|---|---|
| 前端 | Vue `3.5.44` + TypeScript `5.9.3` + Vite `8.3.4`（rolldown）+ `vue-tsc 3.3.13`；原生 `fetch`，无路由/状态库/UI 库 |
| 统一网关 | Python 3.10.9 + FastAPI `0.150.0` + Starlette `1.7.0` + **Pydantic v2** + httpx `0.28.1` + Uvicorn `0.54.0` + PyYAML `6.0.3` |
| 模型服务 | Flask `3.1.3` 单进程（`debug=False`），每模块一个 `api_flask_server.py` + `*_predict_service.py` |
| 大模型 | `openai>=1.0` SDK（OpenAI 兼容协议）→ DeepSeek `deepseek-chat`，可切 Qwen / 智谱；`python-dotenv` 管理密钥 |
| 算法 | torch `2.14.1+cpu`、transformers `4.57.6`（`bert-base-chinese`）、scikit-learn `1.7.2`、fasttext-wheel `0.9.2`、jieba、pandas、numpy |
| 旧版页面 | 各模块仍保留 Streamlit `app_streamlit.py`（`py3109` 环境未安装 Streamlit，主前端为 Vue） |
| 运行环境 | Windows 11、CPU-only（Ryzen 7 8845H / 31.3 GB）、Python 3.10.9（conda `py3109`）、Node 24 + pnpm 11、`venv-gateway` 网关依赖目录 |

---

## 三、系统架构

### 3.1 运行拓扑（当前默认，局域网）

```text
浏览器（同网段成员）
  │  http://192.168.12.54:5173
  ▼
Vite 开发服务器 :5173（Vue 3 SPA）
  │  fetch  /api/v1/models · /classify · /compare
  ▼
FastAPI 统一网关 :8000（serving/gateway，不 import torch/transformers）
  ├── Flask worker :8101  随机森林
  ├── Flask worker :8103  fastText
  ├── Flask worker :8104  BERT
  ├── Flask worker :8105  大模型 → DeepSeek / Qwen / 智谱（外部 API）
  └── Flask worker :8106  蒸馏 BiLSTM
        （06-prune 无在线服务，仅离线实验）
```

- 前端只访问网关，不直接访问 worker；worker 地址、超时、错误归一化由网关统一管理。
- 网关通过 `serving/config/workers.yaml` 调用 worker，通过 `models.yaml` 维护模型清单。
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
├── 01-rf/                    随机森林：config / 预处理 / 训练 / 预测服务 / Flask / Streamlit
├── 02-fasttext/              fastText 四模型 + 预测服务 / Flask / Streamlit
├── 03-bert/                  BERT 微调、评估、预测服务、量化（模型量化_DQ.py）、log/
├── 04-LLM/                   DeepSeek/Qwen/智谱脚本、llm_predict_service、Flask、.env(.example)
├── 05-distill/               教师/学生模型、model_distll.py(v1)、distill_v2.py(v2/v2b)、cache/
├── 06-prune/                 剪枝/评估脚本（无在线服务）
├── frontend/                 Vue 3 SPA：src/{App.vue,api.ts,types.ts,main.ts,style.css}
│                             package.json  vite.config.ts  index.html  dist/  README.md
├── serving/                  统一服务层
│   ├── config/               workers.yaml  models.yaml  processes.yaml
│   ├── gateway/              FastAPI 网关（main.py / routers/ / errors / settings / probes / stats）
│   ├── models/               registry.py  http_adapter.py（worker 调用适配）
│   ├── services/             classification.py（分类编排）
│   ├── scripts/              stage2.ps1  validate_config.py  verify_stage2.py
│   │                         verify_stage3.py  mock_worker.py
│   ├── logs/  run/           运行日志与状态（gitignore）
│   └── README.md             服务能力与边界
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

- 浏览器打开 **`http://192.168.12.54:5173`**；同网段成员使用同一地址。
- `启动TMF.bat` 是幂等的：后端已在运行会复用；前端已在运行（5173 上探测到 TMF 页面）会跳过，不会报 `Port 5173 is already in use`。
- 若其他程序占用 5173，脚本会明确报错；先关闭占用进程或运行停止脚本。
- 局域网访问需要在 Windows 防火墙放行 TCP `5173`、`8000`、`8101`、`8103~8106` 入站。

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
| Vue 前端（Vite） | `http://192.168.12.54:5173` | `strictPort: true`，端口占用直接失败 |
| FastAPI 网关 | `http://192.168.12.54:8000` | 浏览器与前端唯一入口 |
| RF worker | `http://192.168.12.54:8101` | 随机森林 |
| fastText worker | `http://192.168.12.54:8103` | 8102 未使用 |
| BERT worker | `http://192.168.12.54:8104` | fp32 `bert.pkl` |
| LLM worker | `http://192.168.12.54:8105` | 调用外部 Provider，可能计费 |
| 蒸馏 worker | `http://192.168.12.54:8106` | v2b 学生模型 |
| 剪枝 | — | 无在线服务，仅离线脚本 |

> 地址来源：`serving/config/processes.yaml`（网关）、`serving/config/workers.yaml`（worker 调用）、各模块 `api_flask_server.py`（监听）、`frontend/vite.config.ts` + `package.json`（前端）、`frontend/src/api.ts`（API 基址）。**当前绑定的是具体网卡地址而非 `0.0.0.0`**，换机器/换 IP 时需同步修改；如需自适应可改为 `0.0.0.0`。

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
- CORS 默认只允许 `http://192.168.12.54:5173`；`TMF_CORS_ORIGINS` 可追加明确来源（禁止 `*`）。
- `/compare` 允许部分模型失败：返回 `summary{total,ok,failed,status}` + 逐模型 `results`。

### 8.2 模型 worker（内部契约）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/predict_api` | `{"title": "..."}` → `{"title": "...", "pred_class": "sports"}` |
| GET | `/healthz` | 进程存活 |
| GET | `/readyz` | 模型已加载（LLM 仅表示本地服务与客户端已初始化，**不验证密钥**） |
| GET | `/metadata` | 稳定 `model_id` / 展示名（版本当前为 `null`） |

### 8.3 大模型服务

- Provider 由 `TMF_LLM_PROVIDER=deepseek|qwen|zhipu` 切换，密钥只从 `04-LLM/.env` 读取（已 gitignore）。
- 总时间预算 12 s（小于网关对 8105 的 15 s 超时），SDK 自动重试关闭；输出做 10 类白名单校验 + 至多一次重试。
- 前端默认不勾选大模型；真实调用可能产生费用。集成测试使用 `serving/scripts/mock_worker.py` 或 `04-LLM/verify_llm_timeout.py`（本地 mock 上游），**不访问真实 Provider**。

---

## 九、验证与测试

```powershell
# 后端验收（先 start）
E:\AIModels\test\TouManFen\venv-gateway\Scripts\python.exe serving\scripts\verify_stage2.py
E:\AIModels\test\TouManFen\venv-gateway\Scripts\python.exe serving\scripts\verify_stage3.py --llm-mode skip

# 前端类型检查 + 生产构建
cd frontend
pnpm type-check
pnpm build
```

- `verify_stage2.py`：网关 healthz/readyz/models/stats/request-id + 5 个 worker 只读端点 + 4 个本地模型旧 `/predict_api` 契约（脚本含 **35 项**检查；[v2 档案](docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md) 记录 2026-10-20 RESULT PASS / exit 0；**本次文档核对未复跑**）。
- `verify_stage3.py`：分类/对比/边界/错误码/权限边界；`--llm-mode skip` 不触达 LLM，`--llm-mode mock` 配合 `mock_worker.py`（同日 mock 模式 RESULT PASS；skip 模式为历史/前序记录）。
- 前端验收：`http://192.168.12.54:5173` 打开后模型列表加载、单模型分类、多模型对比、错误提示与请求编号。**当前证据到接口层与无头浏览器冒烟为止**（无头 Edge 曾检查 DOM 出现模型名、网关日志出现 `GET /api/v1/models 200`，见 [前端归档](docs/前端开发全过程技术归档.md)）；未做真实浏览器人工视觉验收，`compare` 的界面点击路径也未做浏览器验收。
- 历史运行痕迹（2026-10-20）：17:09–17:10 全量启动 5 个 worker + 网关 → 18:14 最后一条请求 → 18:17 `stage2.ps1 -Action stop` 输出"全部已停止，状态文件已清理"；`serving/logs/gateway.jsonl` 无 `level=ERROR`、无 500。

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
| `TMF_CORS_ORIGINS` | 追加 CORS 来源（逗号分隔，禁止 `*`） | `serving/gateway/main.py` |
| `TMF_DEFAULT_MODEL` / `TMF_MAX_TITLE_CHARS` | 默认模型 / 标题上限 | 覆盖 `serving/config/models.yaml` |
| `TMF_WORKERS_FILE` / `TMF_MODELS_FILE` / `TMF_CLASS_FILE` | 覆盖配置文件路径 | 见 `serving/gateway/settings.py` |
| `TMF_PROBE_TIMEOUT_MS` / `TMF_STATUS_CACHE_MS` | worker 探测超时 / 状态缓存 | 覆盖 `workers.yaml` |
| `VITE_API_BASE_URL` | 前端 API 基址 | 默认 `http://192.168.12.54:8000`（`.env.example`） |

---

## 十一、关键约束与设计决策

1. **前端只访问网关**：worker 地址、超时、错误归一化集中在网关；网关不 import torch/transformers。
2. **统一契约**：网关 `/api/v1/*` 返回 `request_id`；worker 统一 `POST /predict_api` + `title` / `pred_class`。
3. **配置集中在 `serving/config/*.yaml`**：类别唯一来源 `class.txt`；启动前 `validate_config.py` 校验（端口一致性、敏感字段禁止、重复键拒绝）。
4. **地址硬编码绑定网卡**：当前为 `192.168.12.54`（非 `0.0.0.0`）。IP 变化时需同步：`processes.yaml`、`workers.yaml`、5 个 `api_flask_server.py`、`vite.config.ts`、`package.json`、`api.ts`、`.env.example`、`启动TMF.bat` 探测地址。
5. **CPU-only**：BERT 全量微调 1 epoch 约 5.3 小时；训练产物/断点均在本地，不随仓库分发。
6. **版本锁定**：`numpy<2`、`transformers<5`、`fasttext-wheel` 三条硬约束；`scikit-learn==1.7.*` 与 Python 3.10 组合。
7. **网关启动方式**：本机 `venv-gateway` 的 Python 存在 `_ssl` DLL 加载故障，`stage2.ps1` 用 `py3109` 执行网关并从 `venv-gateway\Lib\site-packages` 注入依赖；worker 仍用 `py3109`。
8. **Windows 启停脚本**：BAT 使用纯 ASCII + CRLF；前端启动带 5173 探测（幂等）；停止脚本按 PID/命令行归属校验，不按镜像名批量杀进程。
9. **局域网 HTTP 不是安全上下文**：`crypto.randomUUID`、`crypto.subtle`、`navigator.clipboard` 等 `[SecureContext]` API 不可用；前端 `requestId()` 已实现「`randomUUID` → `getRandomValues` → 时间戳」三级回退。
10. **数据口径**：主口径为未清洗数据；清洗集重训与蒸馏 v2b 属附加实验，结论单独标注。

---

## 十二、已知限制与未完成

- **非生产**：无鉴权、无 HTTPS、无用户体系、无数据库/持久化、无缓存/队列/批量分类、无 Docker/CI；网关与 worker 均为开发服务器单进程。
- **LLM 未系统评估**：仅有样例调用与 16 次网关成功调用（2026-10-20 16:22–18:06，0.5–4.0 s，见 `serving/logs/gateway.jsonl`）；准确性、费用、稳定性未做统计评估；`/readyz` 不代表供应商当前可用。
- **热搜业务未实现**：按用户要求延期（见 `TODO-联通清单.md`）。
- **剪枝不压缩体积**：非结构化剪枝 + `prune.remove()` 固化后仍是稠密存储，只提升稀疏度。
- **量化模型未上服务**：`quantization_model.pkl` 仅离线评估，在线集合仍是 fp32 BERT。
- **地址绑定具体网卡**：IP 变化会直接导致服务启动/访问失败（见第十一节第 4 条）。
- **无 Git 仓库**：截至 2026-10-20 晚项目目录仍未 `git init`（工作区无 `.git`），无法用 `git diff` 复核历史；首次提交前需确认 `.gitignore` 已排除模型/密钥/数据副本。
- **前端未做真实浏览器视觉验收**：仅接口层、无头浏览器冒烟与构建验证；桌面/窄屏观感、交互细节需人工检查；`compare` 的界面点击路径同样未做浏览器验收。
- **停止脚本**：后端 stop 已于 2026-10-20 18:17 经 `serving/logs/stage2-script.log` 确认"全部已停止，状态文件已清理"；前端窗口关闭按窗口标题匹配，仍建议在普通桌面会话复测一次。

---

## 十三、常见问题排查

1. **5173 打不开 / 端口被占**：重新执行 `启动TMF.bat` 会自动跳过已在运行的前端；若被其他程序占用，脚本会报错，先关闭占用进程或运行 `停止TMF.bat`。
2. **页面提示“暂时无法连接 TMF 后端”**：① 先确认网关：`curl http://192.168.12.54:8000/api/v1/healthz`；② 打开浏览器控制台，若有 `crypto.randomUUID is not a function` 之类 `TypeError`，说明是局域网 HTTP 非安全上下文问题（已修复，需强制刷新页面）；③ 确认页面与 API 都走局域网地址，不要混用 `127.0.0.1`；④ 确认 CORS 来源与 `TMF_CORS_ORIGINS`。
3. **网关连不上 worker / `readyz` 有 down**：查看 `serving/logs/gateway.err.log` 与 `serving/logs/workers/*.err.log`；确认 worker 端口与 `workers.yaml` 一致；`stage2.ps1 -Action status` 查看归属与状态。
4. **网关启动不 ready（`_ssl`）**：确认使用 `py3109` 且 `venv-gateway\Lib\site-packages` 存在；查看 `serving/logs/gateway.err.log`；不要用 `taskkill /IM python.exe` 清场。
5. **fastText 预测报 `Unable to avoid copy...`**：numpy 2.x 兼容问题，执行 `python tools/patch_fasttext_numpy2.py` 或安装 `numpy<2`。
6. **BERT 报 `batch_encode_plus` 不存在**：`transformers` 装到了 5.x，降到 `<5`。
7. **`pip install fasttext` 失败**：改用 `fasttext-wheel`。
8. **LLM 返回 401 / 超时**：检查 `04-LLM/.env` 的 `DEEPSEEK_API_KEY`（或对应 Provider）；联调优先用 mock。
9. **局域网其他成员打不开**：Windows 防火墙放行 5173/8000/8101/8103~8106 入站；确认同网段、IP 正确、服务绑定 `192.168.12.54`。
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
| [docs/前端开发全过程技术归档.md](docs/前端开发全过程技术归档.md) | 前端实现过程、决策、踩坑、复现 |
| [docs/PROJECT_LEARNING_GUIDE.md](docs/PROJECT_LEARNING_GUIDE.md) | 全项目工程实践与技术学习档案 |
| [docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md](docs/PROJECT_LEARNING_GUIDE_v2_工程化改造与集成验证.md) | 服务层工程化与集成验证 |
| [docs/阶段0-工程基线报告.md](docs/阶段0-工程基线报告.md) | 环境、依赖、模型基线 |
| [TODO-联通清单.md](TODO-联通清单.md) | 待办与历史联通记录（2026-10-20 晚已按当前状态回填；历史段落保留） |

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

---

## 十六、安全提示

- `04-LLM/.env` 含真实 API Key，已 gitignore；本工程源码中不再有明文密钥。历史 Key 曾暴露，建议到各平台重置。
- **外部资料仍有历史密钥**：`../参考资料/03-笔记/05_LLM实现.md` 与 `../参考代码/gzai6_tmf/04-LLM/`（含 `.env` 与平台示例脚本）不属本工程源码，其中仍含课程阶段的明文 key（联调记录显示旧 key 曾 401，仍按敏感信息处理）；本次未修改这些资料，对外分发/上传前需清理或脱敏。
- **代码与第三方材料的所有权区分**：项目代码归项目所有者所有，并已由所有者确认可上传；`PretrainedModel/`、`数据/`、`参考资料/` 等数据集、预训练模型与课程资料的来源与许可**未在本工程内核对**，分发/对外发布前需单独确认，本工程不作授权声明。
- 服务当前绑定局域网地址且**无鉴权**：同网段任何人都能调用 5 个模型（含 LLM 接口，会产生真实费用）。若只需本机使用，可把地址改回 `127.0.0.1`；若只需暴露前端/网关，可只让网关监听局域网、worker 留在回环地址。
- 代码与模型数据均在本机，训练产物（主口径约 6.7 GB；5 个 `save_model/` 全部文件实测约 10.86 GB）、预训练模型（9.27 GB）、`.env` 均不入库；首次 `git init` 提交前用 `git status` 复查。

---

> 本 README 于 2026-10-20 按当前工程状态系统性重写。指标与耗时均来自本机实测；环境/版本差异可能导致结果不同，以 `docs/实测指标.md` 与各模块日志为准。
