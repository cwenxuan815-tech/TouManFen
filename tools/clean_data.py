# -*- coding: utf-8 -*-
"""
数据去重清洗（只清洗训练集，保留 dev/test 原样以便与历史指标可比）

清洗规则：
  1. 训练集内「去空白归一化标题」重复的样本：只保留第一次出现的那条；
  2. 同一标题在训练集里出现多个不同标签的冲突组（噪声/标注错误）：整组丢弃；
  3. 训练集中与 dev/test 重复的样本：丢弃（防止训练集泄漏到评估集，实测 test 有 110 条这类样本）；
  4. dev/test 内部重复只统计、不删除（保持评估集规模不变）。

产出：
  - 00-data/data/train.clean.txt          清洗后的训练集
  - 00-data/data/clean_report.md          清洗报告
  - 05-distill/cache/train_keep_idx.npy   保留样本在原始 train.txt 中的行索引
                                           （用于复用已缓存的教师 logits，避免重算 2 小时）

用法：
    python tools/clean_data.py
"""
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "00-data", "data")
CACHE_DIR = os.path.join(PROJECT_ROOT, "05-distill", "cache")


def norm(t):
    """去空白归一化：中文标题里的空格多为排版差异"""
    return re.sub(r"\s+", "", t)


def load(name):
    rows = []
    for line in open(os.path.join(DATA_DIR, name), encoding="utf-8"):
        line = line.rstrip("\n")
        if line:
            t, l = line.split("\t")
            rows.append((t, int(l)))
    return rows


def main():
    train = load("train.txt")
    dev = load("dev.txt")
    test = load("test.txt")
    print(f"原始：train={len(train)} dev={len(dev)} test={len(test)}")

    dev_keys = {norm(t) for t, _ in dev}
    test_keys = {norm(t) for t, _ in test}

    # 冲突组：同名不同标签
    labels_by_key = defaultdict(set)
    for t, l in train:
        labels_by_key[norm(t)].add(l)
    conflict_keys = {k for k, s in labels_by_key.items() if len(s) > 1}

    kept_idx, kept_rows = [], []
    seen = set()
    drop_dup = drop_conflict = drop_leak = 0
    for i, (t, l) in enumerate(train):
        k = norm(t)
        if k in conflict_keys:
            drop_conflict += 1
            continue
        if k in dev_keys or k in test_keys:
            drop_leak += 1
            continue
        if k in seen:
            drop_dup += 1
            continue
        seen.add(k)
        kept_idx.append(i)
        kept_rows.append((t, l))

    out_path = os.path.join(DATA_DIR, "train.clean.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        for t, l in kept_rows:
            f.write(f"{t}\t{l}\n")
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.save(os.path.join(CACHE_DIR, "train_keep_idx.npy"), np.array(kept_idx, dtype=np.int64))

    # 清洗后各类别分布
    cnt = Counter(l for _, l in kept_rows)
    dropped = len(train) - len(kept_rows)
    report = [
        "# 数据清洗报告",
        "",
        f"- 原始训练集：{len(train)} 条",
        f"- 清洗后训练集：**{len(kept_rows)} 条**（删除 {dropped} 条，{dropped/len(train)*100:.2f}%）",
        f"  - 重复标题：{drop_dup} 条",
        f"  - 同标题标签冲突（噪声）：{drop_conflict} 条（涉及 {len(conflict_keys)} 组）",
        f"  - 与 dev/test 重叠（防泄漏）：{drop_leak} 条（dev∩train={len(dev_keys & {norm(t) for t,_ in train})}，"
        f"test∩train={len(test_keys & {norm(t) for t,_ in train})}）",
        "",
        "## 清洗后类别分布",
        "",
        "| 类别 id | 条数 |",
        "|---|---|",
    ]
    for l in sorted(cnt):
        report.append(f"| {l} | {cnt[l]} |")
    report += ["", "## 说明", "",
               "- dev/test 未改动（内部重复：dev 7 条 / test 11 条，占评估集 0.07%~0.11%，为保持可比性保留）。",
               "- 保留样本的原始行索引导出到 `05-distill/cache/train_keep_idx.npy`，"
               "蒸馏时可用 `TMF_USE_CLEAN=1` 复用已缓存教师 logits（无需重跑教师前向）。",
               "- 原始 `train.txt` 未覆盖；如确认要替换，把 `train.clean.txt` 改名或改配置指向即可。"]
    with open(os.path.join(DATA_DIR, "clean_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    print(f"清洗完成：保留 {len(kept_rows)} 条（删除 {dropped}：重复 {drop_dup} / 冲突 {drop_conflict} / 泄漏 {drop_leak}）")
    print(f"  -> {out_path}")
    print(f"  -> {os.path.join(DATA_DIR, 'clean_report.md')}")
    print(f"  -> {os.path.join(CACHE_DIR, 'train_keep_idx.npy')}（{len(kept_idx)} 个索引）")
    print("类别分布：", dict(sorted(cnt.items())))


if __name__ == "__main__":
    main()
