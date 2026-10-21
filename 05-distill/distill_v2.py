# 蒸馏 v2：教师 logits 预计算缓存 + BERT 词嵌入 SVD 初始化 + 多 epoch 训练
#
# 相比 v1（model_distll.py）的三处改进：
#   1. 【关键】学生 embedding 用教师 BERT 的词嵌入矩阵做 SVD 降维初始化（v1 是随机初始化，这是精度崩到 0.80 的主因）
#   2. 教师 logits 一次性预计算并缓存 -> 学生每步省掉教师前向（1.4~2.4s），可以低成本跑多个 epoch
#   3. 学生结构升级：embed 256 / hidden 256 / 3 层双向 LSTM，lr=1e-3，多 epoch + 早停
#
# 用法：
#   python distill_v2.py                    # 全量（首次会先跑教师缓存，约 60~80 分钟）
#   TMF_LIMIT=5000 python distill_v2.py     # 小样本冒烟
#   TMF_EPOCHS=6 TMF_LR=1e-3 python distill_v2.py
import os
import sys
import time
import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

from config import Config
import data_preprocessing as dp
from teacher_bert_model import BertTeacherModel
from student_bilstm_v2 import BiLSTMStudentV2

config = Config()
torch.set_num_threads(8)
random.seed(22); np.random.seed(22); torch.manual_seed(22)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("TMF_CACHE_DIR", os.path.join(MODULE_DIR, "cache"))
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(config.student_save_model), exist_ok=True)

LIMIT = int(os.environ.get("TMF_LIMIT", "0"))          # >0 时只用前 N 条训练样本（冒烟）
EPOCHS = int(os.environ.get("TMF_EPOCHS", "6"))
LR = float(os.environ.get("TMF_LR", "1e-3"))
BATCH = int(os.environ.get("TMF_BATCH", "128"))
T = float(os.environ.get("TMF_T", "2"))
ALPHA = float(os.environ.get("TMF_ALPHA", "0.7"))
EMB_DIM = int(os.environ.get("TMF_EMB_DIM", "256"))
HIDDEN = int(os.environ.get("TMF_HIDDEN", "256"))
LAYERS = int(os.environ.get("TMF_LAYERS", "3"))
PATIENCE = int(os.environ.get("TMF_PATIENCE", "2"))
LOG = os.environ.get("TMF_DISTILL_LOG", os.path.join(MODULE_DIR, "log", "train_distill_v2.log"))
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt(sec):
    sec = int(max(sec, 0))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"


# ---------------------------------------------------------------- 1. 读数据 + 分词
def load_rows(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line:
            t, l = line.split("\t")
            rows.append((t, int(l)))
    return rows


def encode(rows, tok, max_len):
    ids, masks = [], []
    for i in range(0, len(rows), 512):
        chunk = [r[0] for r in rows[i:i + 512]]
        enc = tok(chunk, padding="max_length", truncation=True, max_length=max_len, return_tensors="np")
        ids.append(enc["input_ids"].astype(np.int32))
        masks.append(enc["attention_mask"].astype(np.int8))
    return np.vstack(ids), np.vstack(masks), np.array([r[1] for r in rows], dtype=np.int64)


t0 = time.time()
log("=" * 70)
log(f"蒸馏 v2 启动 | epochs={EPOCHS} lr={LR} batch={BATCH} T={T} alpha={ALPHA} "
    f"embed={EMB_DIM} hidden={HIDDEN} layers={LAYERS} limit={LIMIT or '全量'}")
train_rows = load_rows(config.train_datapath)
if LIMIT > 0:
    train_rows = train_rows[:LIMIT]
dev_rows = load_rows(config.dev_datapath)
log(f"训练集 {len(train_rows)} 条 / 验证集 {len(dev_rows)} 条 | 分词中...")
train_ids, train_masks, train_y = encode(train_rows, dp.bert_tokenizer, config.max_length)
dev_ids, dev_masks, dev_y = encode(dev_rows, dp.bert_tokenizer, config.max_length)
log(f"分词完成 {fmt(time.time() - t0)} | train_ids {train_ids.shape}")

# ---------------------------------------------------------------- 2. 教师 logits 预计算缓存
cache_tag = f"n{len(train_rows)}"
logits_path = os.path.join(CACHE_DIR, f"teacher_logits_{cache_tag}.npy")
ids_path = os.path.join(CACHE_DIR, f"train_ids_{cache_tag}.npy")
masks_path = os.path.join(CACHE_DIR, f"train_masks_{cache_tag}.npy")
y_path = os.path.join(CACHE_DIR, f"train_y_{cache_tag}.npy")
np.save(ids_path, train_ids); np.save(masks_path, train_masks); np.save(y_path, train_y)

if os.path.exists(logits_path):
    log(f"教师缓存已存在，直接读取：{logits_path}")
    teacher_logits = np.load(logits_path)
else:
    log("加载教师模型并预计算 logits（一次性，可复用）...")
    teacher = BertTeacherModel().to(config.device)
    teacher.load_state_dict(torch.load(config.teacher_save_model, map_location=config.device))
    teacher.eval()
    out = np.empty((len(train_y), config.classname_len), dtype=np.float16)
    t1 = time.time()
    with torch.no_grad():
        for i in tqdm(range(0, len(train_y), 128), desc="教师前向"):
            ids = torch.from_numpy(train_ids[i:i + 128]).long().to(config.device)
            msk = torch.from_numpy(train_masks[i:i + 128]).long().to(config.device)
            out[i:i + 128] = teacher(ids, msk).cpu().numpy().astype(np.float16)
    teacher_logits = out
    np.save(logits_path, teacher_logits)
    log(f"教师 logits 缓存完成 {fmt(time.time() - t1)} -> {logits_path}")
teacher_logits = teacher_logits.astype(np.float32)
log(f"教师缓存就绪 | shape={teacher_logits.shape}")

# ---------------- 2.5 使用清洗后的训练集（TMF_USE_CLEAN=1） ----------------
# 说明：清洗只删样本、不改顺序，因此可以用「原始行索引」直接过滤已缓存的教师 logits，无需重跑教师前向。
USE_CLEAN = os.environ.get("TMF_USE_CLEAN", "0") == "1"
if USE_CLEAN:
    keep_path = os.environ.get("TMF_KEEP_IDX", os.path.join(CACHE_DIR, "train_keep_idx.npy"))
    if not os.path.exists(keep_path):
        raise FileNotFoundError(f"未找到清洗索引 {keep_path}，请先运行 python tools/clean_data.py")
    keep_idx = np.load(keep_path).astype(np.int64)
    log(f"使用清洗后的训练集：{len(keep_idx)} 条（原始 {len(train_y)} 条；按索引复用教师缓存，不重算教师）")
    train_ids = train_ids[keep_idx]
    train_masks = train_masks[keep_idx]
    train_y = train_y[keep_idx]
    teacher_logits = teacher_logits[keep_idx]

# ---------------------------------------------------------------- 3. 用教师词嵌入初始化学生 embedding
emb_path = os.path.join(CACHE_DIR, f"embed_init_{EMB_DIM}.npy")
if os.path.exists(emb_path):
    init_emb = torch.from_numpy(np.load(emb_path))
    log(f"读取 embedding 初始化缓存：{emb_path} {tuple(init_emb.shape)}")
else:
    log("用教师 BERT 词嵌入做 SVD 降维，生成学生 embedding 初始化...")
    t1 = time.time()
    teacher_for_emb = BertTeacherModel()
    W = teacher_for_emb.bert_model.embeddings.word_embeddings.weight.detach().float()  # [V, 768]
    U, S, _ = torch.svd_lowrank(W, q=EMB_DIM, niter=4)
    proj = U * S                                                                        # [V, EMB_DIM]
    proj = proj / (proj.norm(dim=1, keepdim=True) + 1e-8)                               # 行归一化
    init_emb = proj.contiguous()
    np.save(emb_path, init_emb.numpy())
    log(f"SVD 初始化完成 {fmt(time.time() - t1)} | {tuple(init_emb.shape)} | 行范数={init_emb.norm(dim=1).mean():.3f}")

# ---------------------------------------------------------------- 4. 训练学生
student = BiLSTMStudentV2(
    vocab_size=len(dp.bert_tokenizer), class_len=config.classname_len,
    embedding_dim=EMB_DIM, hidden_size=HIDDEN, num_layers=LAYERS,
    dropout=0.3, init_embedding=init_emb).to(config.device)
# 可从已有学生权重继续训练（例如 v2 已训 6 epoch，再续训 1~2 epoch）
INIT_STUDENT = os.environ.get("TMF_INIT_STUDENT", "")
if INIT_STUDENT and os.path.exists(INIT_STUDENT):
    student.load_state_dict(torch.load(INIT_STUDENT, map_location=config.device))
    log(f"从已有学生模型继续训练：{INIT_STUDENT}")

n_param = sum(p.numel() for p in student.parameters() if p.requires_grad)
log(f"学生参数量 {n_param/1e6:.2f}M（v1 为 6.65M）")

opt = torch.optim.AdamW(student.parameters(), lr=LR, weight_decay=0.01)
ce = nn.CrossEntropyLoss()
best_f1, bad_epochs = 0.0, 0
steps_per_epoch = (len(train_y) + BATCH - 1) // BATCH


def evaluate(ids_np, masks_np, y_true):
    student.eval()
    preds = []
    with torch.no_grad():
        for i in range(0, len(y_true), 256):
            ids = torch.from_numpy(ids_np[i:i + 256]).long().to(config.device)
            msk = torch.from_numpy(masks_np[i:i + 256]).long().to(config.device)
            preds += student(ids, msk).argmax(1).cpu().tolist()
    return (f1_score(y_true, preds, average="macro"), accuracy_score(y_true, preds),
            precision_score(y_true, preds, average="macro"), recall_score(y_true, preds, average="macro"))


log(f"开始训练：{EPOCHS} epochs × {steps_per_epoch} steps")
t_train = time.time()
for epoch in range(1, EPOCHS + 1):
    student.train()
    perm = np.random.permutation(len(train_y))
    total_loss = 0.0
    t_ep = time.time()
    for s in range(steps_per_epoch):
        idx = perm[s * BATCH:(s + 1) * BATCH]
        if len(idx) == 0:
            continue
        ids = torch.from_numpy(train_ids[idx]).long().to(config.device)
        msk = torch.from_numpy(train_masks[idx]).long().to(config.device)
        y = torch.from_numpy(train_y[idx]).to(config.device)
        t_logits = torch.from_numpy(teacher_logits[idx]).to(config.device)

        logits = student(ids, msk)
        hard = ce(logits, y)
        soft = F.kl_div(F.log_softmax(logits / T, dim=-1),
                        F.log_softmax(t_logits / T, dim=-1),
                        reduction="batchmean", log_target=True) * (T * T)
        loss = (1 - ALPHA) * hard + ALPHA * soft

        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(student.parameters(), 5.0)
        opt.step()
        total_loss += loss.item()

        if (s + 1) % 200 == 0 or (s + 1) == steps_per_epoch:
            elapsed = time.time() - t_ep
            speed = elapsed / (s + 1)
            log(f"  epoch {epoch}/{EPOCHS} step {s+1}/{steps_per_epoch} "
                f"loss={loss.item():.4f} | {speed:.3f}s/step | epoch ETA {fmt(speed*(steps_per_epoch-s-1))}")

    f1, acc, p, r = evaluate(dev_ids, dev_masks, dev_y)
    log(f"** epoch {epoch} 完成：dev f1={f1:.4f} acc={acc:.4f} p={p:.4f} r={r:.4f} | "
        f"平均loss={total_loss/max(steps_per_epoch,1):.4f} | 用时 {fmt(time.time()-t_ep)}")
    if f1 > best_f1:
        best_f1 = f1
        bad_epochs = 0
        torch.save(student.state_dict(), config.student_save_model)
        log(f"** 保存学生模型（最佳 f1={best_f1:.4f}）-> {config.student_save_model}")
    else:
        bad_epochs += 1
        if bad_epochs >= PATIENCE:
            log(f"** 连续 {bad_epochs} 个 epoch 未提升，早停")
            break

# ---------------------------------------------------------------- 5. 测试集评估
log(f"训练结束 | 最佳 dev f1={best_f1:.4f} | 总用时 {fmt(time.time()-t_train)}")
test_rows = load_rows(config.test_datapath)
test_ids, test_masks, test_y = encode(test_rows, dp.bert_tokenizer, config.max_length)
student.load_state_dict(torch.load(config.student_save_model, map_location=config.device))
t0 = time.time()
f1, acc, p, r = evaluate(test_ids, test_masks, test_y)
log(f"独立 test 集（{len(test_y)} 条，{fmt(time.time()-t0)}）：f1={f1:.4f} acc={acc:.4f} p={p:.4f} r={r:.4f}")
log(f"学生模型：{config.student_save_model}  {os.path.getsize(config.student_save_model)/1e6:.1f} MB")
