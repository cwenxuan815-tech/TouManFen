# TODO · 联通清单

> **状态回填（2026-10-22 最终）**：本清单写于工程骨架阶段（2026-10-19 前后），其中绝大多数阻塞项已完成。2026-10-21 完成适配层/worker 契约/网络配置重构；2026-10-22 完成 D5 SSE、D6 B8/B12/F10/F11 与 B9/B10/B11/D1/D2/F14/D3/D4、F15 启动来源/cached 回退。当前能力、验证边界与启动方式以 [README.md](README.md)、[v6 学习档案](docs/PROJECT_LEARNING_GUIDE_v6_全链路交付与隔离验收.md) 与专项文档为准；**B8 LLM 仍 missing、D7 未执行**，本清单保留历史/待办记录。
> 状态标记：☐ 未开始 / ◐ 部分完成 / ☑ 已完成
> 证据口径：☑ 表示"存在可追溯记录"；"已实现"与"本次重新验证"是两件事，正文中分别注明。

## 一、原阻塞项（链路已跑通）

- ☑ **生成训练产物**：骨架阶段五个 `save_model/` 只有 `.gitkeep`；2026-10-16~19 已全部产出，2026-10-20 核对文件仍存在：
  - ☑ `01-rf/rf_train.py`：`rf.pkl` 1,583,395,426 B + `tfidf.pkl`，训练 628 s，独立 test macro-F1 **0.8302**
  - ☑ `02-fasttext/ft_train.py`：4 个模型，主口径最优 `word_auto_train.pkl`（451,244,070 B），test **0.9173**
  - ☑ `03-bert/bert_train_and_eval.py`（原"下一步"已完成）：B 方案全量微调，1 epoch / 2813 步 / 5 h 20 m，`bert.pkl` 409,169,600 B（≈390.2 MiB）+ `checkpoint.pt`，独立 test **0.9307**
  - ☑ `05-distill` / `06-prune`：教师模型（`teacher_bert.pkl`）、蒸馏学生（`student_v1/v2/v2b`）、剪枝前后模型均已生成
- ☑ **模型交接脚本**：`tools/handoff_models.py` 已跑通（把 `bert.pkl` 复制成 `06-prune/before_prune_model.pkl` 与 `05-distill/teacher_bert.pkl`）
- ◐ **`run_all.py` 端到端验证**：脚本仍是"按阶段顺序调用各模块脚本"的骨架，**未执行过完整端到端流水线**，也不包含服务启动；现有产物来自人工分步训练，不能据此认为可一键复现。
- ◐ **`04-LLM` 有效 key 与评估**：`QWEN_API_KEY` / `ZHIPU_API_KEY` 为空；DeepSeek key 在 2026-10-19 Streamlit 联调、以及 2026-10-20 16:22–18:06 经网关的 16 次 `model_call=ok`（0.5–4.0 s）中均实际可用；但 2026-10-20 上午的阶段 0 只做了"非空"静态确认，未系统评估准确性/成本/稳定性；**抽样评估脚本仍未实现**。

## 二、指标与结论（影响交付质量）

- ☑ **BERT 架构决策** → **B 方案（解冻全量微调）已跑完**：
  - 1 epoch / 2813 步 / 5h20m，dev **macro F1 0.9249**（acc 0.9245），独立 test **0.9307**；产物 `03-bert/save_model/bert.pkl`（409,169,600 B）+ `checkpoint.pt`（可续训）
  - 验证轨迹：step500 0.9069 → 1000 0.9160 → 1500 0.9215 → 2000 0.9247 → 2813 0.9249（已收敛）
  - 与资料目标 93.17% 差 0.1 个点（test 口径）；此后 handoff → 量化 → 蒸馏 → 剪枝均已执行（见第五节），本条不再有"下一步"。
- ☐ **统一评估脚本**：`tools/evaluate_all.py` 仍不存在；RF / fastText / BERT / 蒸馏 / 剪枝 / 量化的指标散在训练日志与 [docs/实测指标.md](docs/实测指标.md)，尚未固化成一条命令输出统一表格。
- ☑ **剪枝章节的结论**：已按实测修正为"非结构化剪枝 + `prune.remove()` **不减小体积**（剪枝前后均约 409.2 MB），只提升稀疏度、精度约 -0.13"；见 [docs/实测指标.md](docs/实测指标.md) 第五章与 README「已知限制」。**若要"压缩"结论成立，仍需改结构化剪枝或稀疏导出（未做）。**
- ◐ **资料与代码口径统一**：工程内已统一为 `8101~8106 + POST /predict_api + 字段 title`（见 [serving/config/workers.yaml](serving/config/workers.yaml) 与 5 个 `api_flask_server.py`）；资料 PPTX 的 `8001/8003 + /predict + 字段 text` 只作为历史资料保留，未再回改。
- ◐ **fastText 最优模型选型**：服务端加载 `word_auto_train.pkl`，主口径 test **0.9173**（[docs/实测指标.md](docs/实测指标.md) 第八章）；此前版本曾写"word_auto 0.9132 / char_auto 0.9145"，那是不同轮次的限时随机调参结果（清洗集实验章），不能与主口径直接比较。是否改加载 `char_auto` 仍未决策。

## 三、数据与工程化

- ☑ **数据清洗**：已完成并归档 `train.clean.txt`（178,692 条，删除 0.73%：重复标题 809 / 标签冲突 96 / 防泄漏 403），报告见 [00-data/data/clean_report.md](00-data/data/clean_report.md)；**主口径仍是未清洗的 180,000 条**，清洗集仅用于蒸馏 v2b 与附加实验。
- ☑ **接口/前端联调（2026-10-19，Streamlit 5/5）**：用 Streamlit `AppTest` 真实交互测试，RF / fastText / BERT / LLM(DeepSeek) / 蒸馏五个模块的「前端输入 → HTTP 调后端 → 页面显示预测」全部打通；同时修掉 fastText×numpy2、3 个服务缺参兜底、Flask `debug=True` 孤儿进程、LLM 平台切换与输出校验。详见 [docs/联调记录.md](docs/联调记录.md)。**这是历史记录**：2026-10-20 起主前端为 Vue 3，联调对象改为网关。
- ☐ **Gitee 提交**：`.gitignore` 已就位（排除 `.env`、`save_model/`、`PretrainedModel/`、`process_*`、模块数据副本）；**截至 2026-10-20 工作区仍无 `.git`**，需 `git init` → 首次提交 → 远端推送，提交前用 `git status` 确认没有大文件/密钥。
- ☐ **`PretrainedModel` 瘦身**：该目录 9.27 GB，`distilbart-cnn-12-6`（4.08 GB）与各模型的 `tf_model.h5` / `flax_model.msgpack` 对本项目无用，可删除或移到备份盘（未执行）。

## 四、已完成（2026-10-20：服务层 / 前端 / 启停）

- ☑ **FastAPI 统一网关** `serving/gateway/`：`/api/v1/healthz`、`/readyz`、`/models`、`/stats`、`/classify`、`/compare`；网关只经 HTTP 调用 worker，不加载模型。
- ☑ **5 个 Flask worker**：8101/8103/8104/8105/8106，统一 `POST /predict_api`，并新增只读 `/healthz`、`/readyz`、`/metadata`。
- ☑ **Vue 3 + TypeScript + Vite 前端** `frontend/`：单模型分类、多模型对比、模型就绪矩阵、LLM opt-in、错误与请求编号；默认 `http://127.0.0.1:5173`（LAN 用 `TMF_FRONTEND_HOST` 覆盖）；`dist/` 构建产物存在（2026-10-21 16:29 重建）。
- ☑ **启停与验证脚本**：`serving/scripts/stage2.ps1`（start/stop/status/restart）、`启动TMF.bat` / `停止TMF.bat`、`verify_stage2.py`、`verify_stage3.py`（skip/mock）、`mock_worker.py`、`04-LLM/verify_llm_timeout.py`。
- ☑ **验证记录（历史，2026-10-20）**：`verify_stage2.py` / `verify_stage3.py --llm-mode mock` / `verify_llm_timeout.py` 均 RESULT PASS、exit 0（见 v2 档案）；同日 18:17 `stage2.ps1 -Action stop` 输出"全部已停止，状态文件已清理"。**2026-10-21 重构后已重新验证，见第五节。**

## 五、已完成（2026-10-21：适配层 / worker 契约 / 网络配置）

- ☑ **统一模型适配层** `serving/adapters/`：`ModelAdapter` 协议 + `HttpWorkerAdapter` + `LlmAdapter`；`ClassificationService` 只依赖协议，具体适配器在 `serving/gateway/main.py` 的 lifespan 装配。
- ☑ **统一 worker 预测契约** `tmf_common/`：`PredictService` 模板（标题校验 → 推理 → 白名单校验 → `pred_class`/`latency_ms`）；5 个 `*_predict_service.py` 统一为 `SERVICE + predict(dict)` 兼容层；`api_flask_server.py` 变为薄壳，原有 `/predict_api` 字段/错误格式保留。
- ☑ **移除固定 IP**：`workers.yaml`、`processes.yaml`、5 个 `api_flask_server.py`、`stage2.ps1`、`启动TMF.bat`、`vite.config.ts`、`package.json`、`api.ts`、`.env.example`、Streamlit/本地客户端、`run_all.py`、`mock_worker.py`、验证脚本均改为环境变量/配置；默认 `127.0.0.1`。
- ☑ **配置校验**：`tmf_common/config.py` 校验 host/port/URL/CORS；`validate_config.py` 输出 `bind/gateway/frontend/cors/worker_urls`；非法变量快速失败。
- ☑ **验证（2026-10-21 本机）**：
  - `verify_refactor_gateway.py`（44 项）RESULT PASS；
  - `verify_refactor_workers.py` RESULT PASS；
  - 真实服务 `verify_stage2.py`（35 项）RESULT PASS；
  - 真实服务 `verify_stage3.py --llm-mode skip` RESULT PASS；
  - 5 个 mock worker 栈 `verify_stage3.py --llm-mode mock` RESULT PASS；
  - `py_compile` 全部改动 Python 通过；前端 `pnpm type-check` / `pnpm build` 通过；
  - `0.0.0.0` 监听 smoke 通过（mock worker）。

## 六、历史记录（保留原文，便于追溯）

### 5.1 已完成（本轮骨架）

- ☑ 新建 `TouManFenClassifier/`，7 模块结构 + 数据落位（train 18万 / dev 1万 / test 1万）
- ☑ 三处 BERT 路径改为工程同级 `PretrainedModel` 自动解析（可用 `TMF_BERT_PATH` 覆盖）
- ☑ 全部 config 路径改为基于 `__file__` 的绝对路径，**任意工作目录可运行**
- ☑ 5 个模块端口分离（8101/8103/8104/8105/8106）+ RF 内网 IP 改回 127.0.0.1
- ☑ 4 个 LLM 脚本的明文 key 外移到 `04-LLM/.env`（`.env` 已 gitignore）
- ☑ 补齐 `requirements.txt`（含 numpy<2 / transformers<5 / fasttext-wheel 三条硬约束）、`.gitignore`、`README.md`
- ☑ 新增 `tools/prepare_data.py`、`tools/handoff_models.py`、`run_all.py`

### 5.2 已完成（2026-10-19 全链路训练与优化实测）

- ☑ 随机森林：全量 18 万训练 628s，独立 test 集 macroF1 **0.8302**（模型 1512MB）
- ☑ fastText：4 个模型 528s，最好 0.9173（词级 auto，451MB）
- ☑ **BERT 全量微调（B 方案）**：1 epoch 5h20m，独立 test 集 **0.9307**（390.2MB，与资料 93.17% 基本一致）
- ☑ **量化 DQ int8**：2.68x 压缩（152.7MB）、推理 2.8x 提速、F1 **0.9263**（-0.44）
- ☑ **剪枝 30% 非结构化**：F1 **0.9294**（-0.13）、体积不变（390.2MB）→ 结论已按实测修正
- ☑ **蒸馏 BiLSTM v1**：2h13m，学生 26.6MB（14.7x 压缩）、推理 23s/万条，F1 **0.8021**（-12.9，不可用）
- ☑ **蒸馏 BiLSTM v2（改进版）**：BERT 词嵌入 SVD 初始化 + 教师 logits 缓存 + 6 epoch，
  test F1 **0.8974**（+9.5 点），36.7MB（10.6x 压缩）、12s/万条
- ☑ **数据去重清洗**：180,000 → **178,692**（删除 0.73%：重复 809 / 标签冲突 96 / 防泄漏 403），
  见 [00-data/data/clean_report.md](00-data/data/clean_report.md)；清洗索引复用教师缓存（省约 2 小时重算）
- ☑ **蒸馏 BiLSTM v2b（续训 + 清洗数据，当前最优学生）**：再训 2 epoch（40m24s），
  dev **0.9022** / test **0.9074**（+1.0 点，突破 0.90），36.7MB、13s/万条；
  `05-distill/config.py` 默认模型已切 `student_v2b.pkl`，接口复测 5/5
- ☑ 模型交接脚本跑通（bert.pkl → 06-prune / 05-distill）
- ☑ 全部指标已回填 [docs/实测指标.md](docs/实测指标.md)（含统一口径总对比表）
- ☑ **清洗集重训 RF + fastText**：RF 0.8302→**0.8314**、fastText 字符-auto 0.9140→**0.9145**，
  其余模型 ±0.001（word_auto -0.0098 属自动调参波动）；清洗对这两个模型无实质影响
- ☑ **最终口径决策**：主口径 = 未清洗数据（服务端模型不变），清洗集作为实验章节呈现；
  详见 docs/实测指标.md 第九、十章，README「口径说明」

## 七、当前待办汇总（2026-10-22 最终）

### 已完成（本阶段）
- ☑ **D5**：SSE 渐进对比、总截止、取消、前端降级（B6/F9）。
- ☑ **D6**：B8 离线基准/PK API、B12 并发/线程边界、F10 竞技场、F11 PK 榜。
- ☑ **最终收尾**：B9 LLM opt-in、B10 后端回归、B11 data_source/快照、D1 文档、D2 演示数据/seed、F14 前端质量、D3 演示脚本。
- ☑ **D4**：隔离 mock/headless 下正常 10 步 + 5 故障场景复验；网络审计 `all_isolated=true`。
- ☑ **F15**：启动来源 `/api/v1/demo/state`、cached 快照回退、非法/缺失快照错误态。
- ☑ **学习档案 v6**：全链路交付/决策/问题/证据复盘；README 索引已同步。

### 仍待处理 / 未验证
- ☐ **B8 真实 LLM 基准**：当前 LLM=missing；需要真实 Provider 授权、费用/稳定性评估后才能补 5 模型 PK，不能用 mock 代替。
- ☐ **真实设备验收**：投影/手机/笔记本人工视觉、键盘/触摸、真实局域网外部机器；F13/D4 目前是 Edge headless 模拟。
- ☐ **真实 LAN/外部 Provider**：用户 stage2 运行状态不在本项目测试范围；外部 LAN、真实网络断开、真实 Provider 均未验收。
- ☐ **D7**：未执行。
- ◐ **历史工程脚本**：`run_all.py` 完整端到端流水线、`tools/evaluate_all.py` 统一评估脚本仍未固化；`PretrainedModel` 瘦身未做（历史待办，非本轮范围）。
- ◐ **旧 D4 非隔离证据**：`artifacts/d4/` 中旧文件仍保留，文档已标注由 `*-isolated` 取代；是否归档/清理待决定。
- ☐ **生产化**：鉴权、HTTPS、生产级数据库/迁移、队列、Docker/CI 未实现。
- 明确未实现、不要记为已具备：calibrated confidence、批量分类、API 缓存/single-flight/级联、热搜、鉴权、生产级数据库/队列、Docker/CI。
- 已实现但不要误读：B4/B7 使用本地 SQLite 存反馈/投票；cached/mock 不进入 live 统计；B8 集成/oracle 是派生指标。
