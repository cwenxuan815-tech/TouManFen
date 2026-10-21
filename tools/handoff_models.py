# -*- coding: utf-8 -*-
"""
模型交接脚本：把 03-bert 训练出的分类模型复制给下游模块。

背景：参考代码里这一步是「手工复制 + 改名」，没有脚本也没有文档，容易卡住。
本脚本把它固化下来。已验证：03-bert 的 BertClassifierModel 与 05-distill 的
BertTeacherModel 结构相同（state_dict 键名均为 bert_model.* / linear.*），
因此可以直接复制 state_dict，无需改代码。

交接关系：
    03-bert/save_model/bert.pkl  ->  06-prune/save_model/before_prune_model.pkl   （剪枝输入）
    03-bert/save_model/bert.pkl  ->  05-distill/save_model/teacher_bert.pkl       （蒸馏教师）

用法：
    python tools/handoff_models.py
"""
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(PROJECT_ROOT, "03-bert", "save_model", "bert.pkl")

TARGETS = [
    os.path.join(PROJECT_ROOT, "06-prune", "save_model", "before_prune_model.pkl"),
    os.path.join(PROJECT_ROOT, "05-distill", "save_model", "teacher_bert.pkl"),
]


def main():
    if not os.path.exists(SOURCE):
        sys.exit(f"[错误] 找不到 03-bert 的模型：{SOURCE}\n"
                 f"        请先运行 03-bert/bert_train_and_eval.py 完成训练。")
    size = os.path.getsize(SOURCE) / 1e6
    print(f"[源] {SOURCE}  ({size:.1f} MB)")
    for dst in TARGETS:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(SOURCE, dst)
        print(f"  -> {dst}")
    print("[完成] 模型交接完毕，现在可以运行 05-distill/model_distll.py 与 06-prune/model_prune.py")


if __name__ == "__main__":
    main()
