#!/usr/bin/env python
"""B3：生成四类**合成**负样本（闲聊 / 代码 / 商品名 / 纯符号）。

用途：
- 为软拒识阈值标定提供「非新闻」输入分布；
- 仅用于 ``tools/eval_reject.py`` 的离线 ROC 评估，不参与训练、不入库。

硬性约束（见 docs/工程实施工作清单_75工时.md §7 B3）：
1. 四类各 300 条，共 1200 条；
2. 固定随机种子（默认 20261021），模板 + 组合**确定性**生成：
   同一种子/同一代码版本重复运行，产物逐字节一致；
3. **不得伪装真实用户数据**：每条记录带 ``meta.synthetic=true``、
   ``meta.generator``、``meta.seed``，不含任何真实用户/采集数据；
4. 文本保持单行（不含换行），避免 fastText ``predict`` 对多行输入报错；
5. 只依赖标准库，不加载模型、不联网。

产物：``artifacts/b3/synthetic_negatives.jsonl``（每行一个 JSON 对象）

    {"text": "...", "kind": "chitchat|code|product|symbols",
     "meta": {"synthetic": true, "generator": "tools/make_negatives.py",
              "seed": 20261021, "kind": "...", "template_id": "...",
              "note": "合成负样本（模板生成），非真实用户数据"}}

用法：
    D:\\Miniconda3\\envs\\py3109\\python.exe tools/make_negatives.py
    ... --seed 20261021 --per-kind 300 --out artifacts/b3/synthetic_negatives.jsonl
    ... --print-samples 3   # 额外打印每类样例，便于人工抽查
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "artifacts" / "b3" / "synthetic_negatives.jsonl"
DEFAULT_SEED = 20261021
DEFAULT_PER_KIND = 300
GENERATOR = "tools/make_negatives.py"
SYNTHETIC_NOTE = "合成负样本（模板/组合确定性生成），非真实用户数据"

KINDS: tuple[str, ...] = ("chitchat", "code", "product", "symbols")

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台按 UTF-8 输出中文
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 极端环境
        pass


# ---------------------------------------------------------------------------
# 1. 闲聊（chitchat）
# ---------------------------------------------------------------------------
CHITCHAT_OPENERS = (
    "今天天气不错",
    "周末打算和朋友去爬山",
    "中午吃什么好呢",
    "最近在追的一部剧挺好看",
    "晚上睡不着怎么办",
    "刚下班好累啊",
    "明天要早起先睡了",
    "你觉得这首歌怎么样",
    "家里猫又把杯子打翻了",
    "好久没喝奶茶了",
    "有点想吃火锅",
    "今天地铁好挤",
    "新买的手机壳到了",
    "楼下新开了家面包店",
    "下午开会开得头疼",
    "不知道该不该换工作",
    "刚看完比赛好激动",
    "想请假出去玩几天",
    "这件衣服好看吗",
    "朋友推荐了一家店",
    "最近在学做饭",
    "周末在家收拾房间",
    "刚洗完澡好舒服",
    "明天要不要一起吃饭",
    "今天走了两万步",
    "窗外下雨了好凉快",
    "新换的壁纸真好看",
    "刚收到快递",
    "头发又长了想剪",
    "在想要不要养只狗",
    "今天咖啡喝多了",
    "晚上准备看个电影",
    "附近公园花开了",
    "上次那家店关门了",
    "在纠结买哪个颜色",
    "刚把作业写完",
    "想出去散散步",
    "今天有点困",
    "室友做了晚饭",
    "计划下个月去旅行",
)

CHITCHAT_CLOSERS = (
    "",
    "，你觉得呢",
    "，有推荐吗",
    "，先这样吧",
    "，哈哈",
    "，好想吃",
    "，明天再说",
    "，有点纠结",
    "，求建议",
    "，晚安",
    "，周末再说",
    "，你呢",
    "，随便聊聊",
    "，好久没见了",
    "，下次一起",
)

# 闭包池 40 × 15 = 600 种组合；固定种子取 300 条，模板 id 记录组合下标。
_CHITCHAT_COMBOS = [
    (opener_index, closer_index)
    for opener_index in range(len(CHITCHAT_OPENERS))
    for closer_index in range(len(CHITCHAT_CLOSERS))
]


def _build_chitchat(rng: random.Random, count: int) -> list[tuple[str, str]]:
    picked = rng.sample(range(len(_CHITCHAT_COMBOS)), count)
    items: list[tuple[str, str]] = []
    for combo_index in picked:
        opener_index, closer_index = _CHITCHAT_COMBOS[combo_index]
        text = CHITCHAT_OPENERS[opener_index] + CHITCHAT_CLOSERS[closer_index]
        items.append((text, f"chitchat-{combo_index}"))
    return items


# ---------------------------------------------------------------------------
# 2. 代码（code）—— 单行片段，覆盖常见语言与 SQL
# ---------------------------------------------------------------------------
CODE_TEMPLATES: tuple[str, ...] = (
    "def {fn}({args}): return {expr}",
    "{var} = {expr}",
    "for i in range({num}): print(i * {num2})",
    "if {cond}: print('{word}')",
    "class {Name}: pass",
    "import {mod}; print({mod_attr})",
    "{obj}.append({expr})",
    "try: {stmt} except {exc} as e: print(e)",
    "SELECT {cols} FROM {table} WHERE {sql_cond};",
    "{lang}: {code} // {comment}",
    "@{decorator} def {fn}({args}): pass",
    "print(f'{word}: {{value}}')",
)

CODE_SLOTS: dict[str, tuple[str, ...]] = {
    "fn": (
        "add", "main", "train", "predict", "load_data", "build_model",
        "compute_loss", "parse_args", "run", "save",
    ),
    "args": (
        "a, b", "x", "data, label", "self", "n=10", "path", "*args, **kwargs",
        "x: int", "y=None", "config",
    ),
    "expr": (
        "a + b", "x * 2", "len(data)", "np.zeros(3)", "model.fit(X, y)",
        "value if ok else 0", "{v: v for v in items}", "sum(nums) / len(nums)",
        "json.dumps(payload)", "None",
    ),
    "var": (
        "x", "result", "data", "model", "config", "items", "total", "score",
        "path", "value",
    ),
    "num": ("10", "100", "3", "42", "7", "256", "0", "1000", "64", "-1"),
    "num2": ("2", "3", "5", "10", "7", "4", "8", "6", "9", "1"),
    "cond": (
        "i > 0", "x is None", "not ready", "len(items) == 0", "score >= 0.5",
        "flag", "i % 2", "name == 'admin'", "done", "err",
    ),
    "word": (
        "hello", "test", "done", "start", "value", "ok", "result", "data",
        "python", "error",
    ),
    "Name": (
        "Model", "Trainer", "Config", "Dataset", "Service", "Base", "Handler",
        "Loader", "Job", "Runner",
    ),
    "mod": (
        "numpy as np", "json", "os", "sys", "math", "random", "time",
        "collections", "re", "itertools",
    ),
    "mod_attr": (
        "array([1, 2])", "dumps({})", "getcwd()", "sqrt(2)", "randint(0, 9)",
        "time()", "Counter([1])", "compile('a')", "chain([1], [2])", "pi",
    ),
    "obj": (
        "items", "scores", "results", "rows", "data", "values", "records",
        "queue",
    ),
    "stmt": ("risky()", "int('x')", "open(path)", "json.loads(text)", "result.call()"),
    "exc": ("ValueError", "KeyError", "Exception", "TypeError", "OSError"),
    "cols": ("id, name", "*", "COUNT(*)", "a, b", "title", "score"),
    "table": ("users", "items", "feedback", "scores", "news"),
    "sql_cond": (
        "id = 1", "score > 0.5", "name IS NOT NULL", "created_at < NOW()",
        "status = 'ok'", "id IN (1, 2, 3)",
    ),
    "lang": ("javascript", "python", "java", "c++", "go", "sql"),
    "code": (
        "const x = 1;", "print('hi')", "int main() { return 0; }",
        "SELECT * FROM t;", "x := 1", "var y = 2;",
    ),
    "comment": ("todo", "fix me", "debug", "test", "tmp"),
    "decorator": ("property", "staticmethod", "app.route('/x')", "dataclass", "cache"),
}


def _build_code(rng: random.Random, count: int) -> list[tuple[str, str]]:
    """按模板枚举全部槽位组合、用固定 rng 打乱后轮流取，避免唯一组合耗尽死循环。

    小模板（如 ``class {Name}: pass`` 只有 10 种）只会贡献其容量内的条数；
    其余名额由大模板补齐，因此结果确定且条数精确。
    """
    import itertools

    queues: list[list[tuple[str, str]]] = []
    for template_index, template in enumerate(CODE_TEMPLATES):
        slot_keys = [key for key in CODE_SLOTS if "{" + key + "}" in template]
        combos = list(itertools.product(*(CODE_SLOTS[key] for key in slot_keys)))
        rng.shuffle(combos)
        queues.append(
            [
                (
                    template.format(**dict(zip(slot_keys, combo))),
                    f"code-{template_index}",
                )
                for combo in combos
            ]
        )

    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    active = list(range(len(queues)))
    while len(items) < count and active:
        for template_index in list(active):
            if len(items) >= count:
                break
            queue = queues[template_index]
            while queue:
                text, template_id = queue.pop(0)
                if text not in seen:
                    seen.add(text)
                    items.append((text, template_id))
                    break
            if not queue:
                active.remove(template_index)
    return items


# ---------------------------------------------------------------------------
# 3. 商品名（product）—— 电商式标题
# ---------------------------------------------------------------------------
# 前缀必须两两不同：组合下标按前缀位编码，重复前缀会产生重复文本。
PRODUCT_PREFIXES = (
    "【新品】", "【热销】", "【限时特惠】", "【官方正品】", "【今日推荐】",
    "【清仓】", "【包邮】", "【赠品】", "【自营】", "【精选】",
)

PRODUCT_NOUNS = (
    "不锈钢保温杯", "无线蓝牙耳机", "纯棉短袖T恤", "双肩背包", "机械键盘",
    "玻璃花瓶", "陶瓷餐盘", "记忆棉枕头", "运动腰包", "折叠雨伞",
    "电动牙刷", "手持风扇", "收纳箱", "香薰蜡烛", "瑜伽垫",
    "数据线", "手机支架", "保温饭盒", "儿童积木", "羽绒服",
)

PRODUCT_SPECS = (
    "500ml", "升级款", "大容量", "316不锈钢", "2024新款", "加厚",
    "轻便", "防泼水", "静音", "可折叠", "透气", "高弹",
    "防水", "耐用", "超大号",
)

PRODUCT_FEATURES = (
    "带盖", "便携", "男女同款", "家用", "学生党必备", "办公室",
    "户外露营", "旅行", "节日礼物", "买一送一", "多色可选", "速干",
)

# 10 × 20 × 15 × 12 = 36000 种组合；固定种子取 300 条，下标可完全复现。
_PRODUCT_TOTAL = (
    len(PRODUCT_PREFIXES)
    * len(PRODUCT_NOUNS)
    * len(PRODUCT_SPECS)
    * len(PRODUCT_FEATURES)
)


def _product_combo(combo_index: int) -> str:
    features_count = len(PRODUCT_FEATURES)
    specs_count = len(PRODUCT_SPECS)
    nouns_count = len(PRODUCT_NOUNS)
    rest, feature_index = divmod(combo_index, features_count)
    rest, spec_index = divmod(rest, specs_count)
    prefix_index, noun_index = divmod(rest, nouns_count)
    return (
        PRODUCT_PREFIXES[prefix_index]
        + PRODUCT_NOUNS[noun_index]
        + " "
        + PRODUCT_SPECS[spec_index]
        + " "
        + PRODUCT_FEATURES[feature_index]
    )


def _build_product(rng: random.Random, count: int) -> list[tuple[str, str]]:
    picked = rng.sample(range(_PRODUCT_TOTAL), count)
    return [(_product_combo(index), f"product-{index}") for index in picked]


# ---------------------------------------------------------------------------
# 4. 纯符号（symbols）—— 无字母/汉字，模拟乱码/表情串
# ---------------------------------------------------------------------------
SYMBOL_ALPHABET = (
    "！？。…—～·、；：（）【】《》〈〉“”‘’"
    "＠＃＄％＾＆＊＿－＋＝｜＼／＜＞"
    "@#$%^&*_-+=|\\/<>~`"
    "★☆◆◇■□▲△●○◎※→←↑↓"
    "①②③❶❷❸"
)


def _build_symbols(rng: random.Random, count: int) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    while len(items) < count:
        clusters = rng.randint(2, 5)
        parts: list[str] = []
        for _ in range(clusters):
            symbol = rng.choice(SYMBOL_ALPHABET)
            parts.append(symbol * rng.randint(1, 4))
        text = "".join(parts)
        if text in seen:
            continue
        seen.add(text)
        items.append((text, "symbols-runs"))
    return items


BUILDERS: dict[str, Callable[[random.Random, int], list[tuple[str, str]]]] = {
    "chitchat": _build_chitchat,
    "code": _build_code,
    "product": _build_product,
    "symbols": _build_symbols,
}


# ---------------------------------------------------------------------------
# 生成与写出
# ---------------------------------------------------------------------------
def generate(seed: int, per_kind: int) -> list[dict[str, object]]:
    """按固定顺序生成四类负样本；同一 seed/per_kind 输出完全一致。"""
    records: list[dict[str, object]] = []
    for kind_index, kind in enumerate(KINDS):
        # 每类使用由主种子派生的独立子种子：类顺序调整不影响其余类的输出。
        kind_seed = seed + kind_index * 1000003
        rng = random.Random(kind_seed)
        for text, template_id in BUILDERS[kind](rng, per_kind):
            records.append(
                {
                    "text": text,
                    "kind": kind,
                    "meta": {
                        "synthetic": True,
                        "generator": GENERATOR,
                        "seed": seed,
                        "kind_seed": kind_seed,
                        "kind": kind,
                        "template_id": template_id,
                        "note": SYNTHETIC_NOTE,
                    },
                }
            )
    return records


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 B3 四类合成负样本（闲聊/代码/商品名/纯符号）"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="固定随机种子")
    parser.add_argument(
        "--per-kind", type=int, default=DEFAULT_PER_KIND, help="每类条数（默认 300）"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 JSONL 路径")
    parser.add_argument(
        "--print-samples", type=int, default=0, help="每类额外打印样例条数"
    )
    args = parser.parse_args(argv)

    if args.per_kind <= 0:
        print("ERROR: --per-kind 必须为正整数")
        return 2

    records = generate(args.seed, args.per_kind)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    write_jsonl(out_path, records)

    counts = Counter(str(record["kind"]) for record in records)
    print(f"seed={args.seed} per_kind={args.per_kind} total={len(records)}")
    for kind in KINDS:
        print(f"  {kind}: {counts.get(kind, 0)}")
    print(f"OUT {out_path}")

    if args.print_samples > 0:
        for kind in KINDS:
            samples = [
                record["text"] for record in records if record["kind"] == kind
            ][: args.print_samples]
            for sample in samples:
                print(f"SAMPLE[{kind}] {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
