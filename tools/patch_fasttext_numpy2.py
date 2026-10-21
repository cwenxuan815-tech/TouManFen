# -*- coding: utf-8 -*-
"""
修复 fasttext 与 NumPy 2.x 的不兼容（Windows / 已有环境里 numpy 被其它库锁到 2.x 时使用）。

问题：fasttext 的 Python 封装用了 `np.array(x, copy=False)`，NumPy 2.x 收紧了 copy 语义，
      调用 model.predict() 会抛：ValueError: Unable to avoid copy while creating an array as requested.
修法：把 `np.array(x, copy=False)` 改成 `np.asarray(x)`（语义等价，允许必要拷贝）。

本脚本自动定位已安装的 fasttext 包并打补丁，可重复执行（幂等），会先备份为 FastText.py.bak。
在新环境里更推荐的做法仍是 `pip install "numpy<2"`（见 requirements.txt）。

用法：
    python tools/patch_fasttext_numpy2.py
"""
import os
import re
import sys

def main():
    try:
        import fasttext
    except ImportError:
        sys.exit("[错误] 当前环境没有安装 fasttext / fasttext-wheel")
    target = os.path.join(os.path.dirname(os.path.abspath(fasttext.__file__)), "FastText.py")
    if not os.path.exists(target):
        sys.exit(f"[错误] 找不到 {target}")

    src = open(target, encoding="utf-8").read()
    hits = re.findall(r"np\.array\((\w+),\s*copy=False\)", src)
    if not hits:
        print("[跳过] 未发现 np.array(..., copy=False)，可能已打过补丁或版本不同")
    else:
        backup = target + ".bak"
        if not os.path.exists(backup):
            open(backup, "w", encoding="utf-8").write(src)
            print(f"[备份] {backup}")
        patched = re.sub(r"np\.array\((\w+),\s*copy=False\)", r"np.asarray(\1)", src)
        open(target, "w", encoding="utf-8").write(patched)
        print(f"[完成] 已替换 {len(hits)} 处 -> {target}")

    # 自检
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("__label__a 你 好 世 界\n__label__b 股 票 上 涨\n" * 50)
    m = fasttext.train_supervised(input=p, verbose=0)
    print("[自检] predict ->", m.predict("你 好"))
    print("[自检] test    ->", m.test(p))


if __name__ == "__main__":
    main()
