# -*- coding: utf-8 -*-
"""
一键流水线骨架（⚠️ 骨架：按顺序调用各模块脚本，尚未端到端验证，按实际产物补全）

设计目标：把「数据准备 → 01-rf → 02-fasttext → 03-bert → 量化 → 05-distill → 06-prune」
串成一条命令。每一步都是真实调用各模块自己的脚本（不重写业务逻辑），
因此某个模块改坏了，本脚本也会在对应步骤停下并保留日志。

用法：
    python run_all.py                # 只跑「不依赖已训练模型」的前半段
    python run_all.py --stage rf     # 只跑到指定阶段
    python run_all.py --list         # 列出所有阶段
"""
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# (阶段名, 工作目录, 脚本, 说明)
STAGES = [
    ("prepare", os.path.join("tools"), "prepare_data.py", "把 00-data/data 同步到各模块 data/"),
    ("rf", os.path.join("01-rf"), "data_preprocessing.py", "RF：jieba 分词预处理"),
    ("rf-train", os.path.join("01-rf"), "rf_train.py", "RF：训练 + 8:2 评估 + 保存模型"),
    ("ft-pre", os.path.join("02-fasttext"), "data_preprocessing.py", "fastText：字符级/词级预处理"),
    ("ft-train", os.path.join("02-fasttext"), "ft_train.py", "fastText：4 个模型（含 auto 调参）"),
    ("bert-train", os.path.join("03-bert"), "bert_train_and_eval.py", "BERT：训练 + 每 100 批验证"),
    ("bert-quant", os.path.join("03-bert"), "模型量化_DQ.py", "DQ 动态量化 + 量化前后评估"),
    # ---- 以下三步依赖上一步的模型产物，交接见 README「模型交接」 ----
    ("handoff", os.path.join("tools"), "handoff_models.py", "把 03-bert 产物复制给 05-distill / 06-prune"),
    ("distill", os.path.join("05-distill"), "model_distll.py", "蒸馏：BERT(教师) -> BiLSTM(学生)"),
    ("prune", os.path.join("06-prune"), "model_prune.py", "剪枝：注意力 q/k/v 全局 30%"),
]

# 服务类（需要单独开窗口，不放进流水线主流程）
SERVICES = [
    ("01-rf", "api_flask_server.py", 8101),
    ("02-fasttext", "api_flask_server.py", 8103),
    ("03-bert", "api_flask_server.py", 8104),
    ("04-LLM", "api_flask_server.py", 8105),
    ("05-distill", "api_flask_server.py", 8106),
]


def list_stages():
    print("可用阶段：")
    for name, cwd, script, desc in STAGES:
        print(f"  {name:<12} {os.path.join(cwd, script):<42} {desc}")
    print("\n服务（各自单独启动）：")
    for cwd, script, port in SERVICES:
        print(f"  [{cwd}] python {script}   -> http://192.168.12.54:{port}/predict_api")


def run_stage(name, cwd, script, desc):
    path = os.path.join(PROJECT_ROOT, cwd, script)
    if not os.path.exists(path):
        print(f"[跳过] {name}: 找不到 {path}")
        return False
    print(f"\n{'=' * 78}\n[{name}] {desc}\n  cwd = {os.path.join(PROJECT_ROOT, cwd)}\n  cmd = {PY} {script}\n{'=' * 78}")
    t0 = time.time()
    rc = subprocess.run([PY, "-u", script], cwd=os.path.join(PROJECT_ROOT, cwd)).returncode
    print(f"[{name}] 退出码 {rc}，用时 {time.time() - t0:.1f}s")
    return rc == 0


def main():
    args = sys.argv[1:]
    if "--list" in args:
        list_stages()
        return
    stop_at = None
    if "--stage" in args:
        stop_at = args[args.index("--stage") + 1]
    for name, cwd, script, desc in STAGES:
        run_stage(name, cwd, script, desc)
        if stop_at and name == stop_at:
            print(f"\n[停止] 已到达指定阶段：{stop_at}")
            return


if __name__ == "__main__":
    main()
