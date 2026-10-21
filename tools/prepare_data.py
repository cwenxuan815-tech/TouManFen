# -*- coding: utf-8 -*-
"""
数据准备脚本：把 00-data/data 下的原始数据同步到各模块需要的 data/ 目录。

为什么需要它：各模块的 data/ 目录内容在 .gitignore 中被排除（避免同一份数据在仓库里存三份），
因此新克隆工程后先跑一次本脚本，再执行各模块的训练/评估脚本。

用法：
    python tools/prepare_data.py
"""
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(PROJECT_ROOT, "00-data", "data")

# 模块内需要哪些数据文件（相对工程根 -> 需要复制的文件名列表）
TARGETS = {
    os.path.join("05-distill", "data"): ["train.txt", "dev.txt", "test.txt", "class.txt"],
    os.path.join("06-prune", "data"): ["dev.txt", "class.txt"],
}


def main():
    if not os.path.isdir(SOURCE_DIR):
        sys.exit(f"[错误] 源数据目录不存在：{SOURCE_DIR}")
    missing = [f for f in ("train.txt", "dev.txt", "test.txt", "class.txt")
               if not os.path.exists(os.path.join(SOURCE_DIR, f))]
    if missing:
        sys.exit(f"[错误] 源数据缺少文件：{missing}")

    for rel_dir, files in TARGETS.items():
        dst_dir = os.path.join(PROJECT_ROOT, rel_dir)
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            src, dst = os.path.join(SOURCE_DIR, f), os.path.join(dst_dir, f)
            shutil.copyfile(src, dst)
            print(f"  复制 {f:<12} -> {rel_dir}")
    print("[完成] 各模块 data/ 目录已就绪")


if __name__ == "__main__":
    main()
