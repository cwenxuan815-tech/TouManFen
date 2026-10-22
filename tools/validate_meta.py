#!/usr/bin/env python
"""B13：离线指标产物 meta 契约与校验。

清单 §6.8 规定所有离线产物必须包含以下 ``meta``（顶层对象或产物文件的
``meta`` 字段）：

    {
      "meta": {
        "dataset": "00-data/data/test.txt",
        "split": "test",
        "sample_size": 10000,
        "generated_at": "2026-10-21T18:00:00+08:00",
        "code_version": "git:<short-sha> | uncommitted",
        "model_versions": {"bert": "bert.pkl", "fasttext": "word_auto_train.pkl"},
        "metric_def": {"f1": "macro", "acc": "top1"},
        "data_kind": "offline"
      },
      "models": []
    }

本模块只做契约校验，不生成 B8 基准产物、不实现 PK API、不生成演示快照。

CLI：
    python tools/validate_meta.py <artifact.json>
      exit 0 = 合法；2 = 契约校验失败；1 = 文件/JSON/用法错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

try:  # 让 Windows 控制台也按 UTF-8 输出中文错误信息
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非交互环境可能不支持
    pass

ALLOWED_DATA_KINDS = ("offline", "demo")
REQUIRED_META_FIELDS = (
    "dataset",
    "split",
    "sample_size",
    "generated_at",
    "code_version",
    "model_versions",
    "metric_def",
    "data_kind",
)
_CODE_VERSION_PATTERN = re.compile(r"^git:[0-9a-f]{7,40}$")


class MetaValidationError(ValueError):
    """meta 不符合 §6.8 契约；message 可直接展示给使用者。"""


@dataclass(frozen=True)
class MetricMeta:
    """通过校验的 meta 契约（供 B8 产物生成器复用）。"""

    dataset: str
    split: str
    sample_size: int
    generated_at: str
    code_version: str
    model_versions: dict[str, str]
    metric_def: dict[str, str]
    data_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "split": self.split,
            "sample_size": self.sample_size,
            "generated_at": self.generated_at,
            "code_version": self.code_version,
            "model_versions": dict(self.model_versions),
            "metric_def": dict(self.metric_def),
            "data_kind": self.data_kind,
        }


def _require_non_empty_str(meta: Mapping[str, Any], field: str) -> str:
    value = meta.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MetaValidationError(f"meta.{field}: 必须是非空字符串")
    return value.strip()


def _require_mapping(meta: Mapping[str, Any], field: str) -> dict[str, str]:
    value = meta.get(field)
    if not isinstance(value, dict) or not value:
        raise MetaValidationError(f"meta.{field}: 必须是非空对象")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise MetaValidationError(f"meta.{field}: key 必须是非空字符串")
        if not isinstance(item, str) or not item.strip():
            raise MetaValidationError(
                f"meta.{field}.{key}: 必须是非空字符串"
            )
        cleaned[key.strip()] = item.strip()
    return cleaned


def _validate_generated_at(value: str) -> str:
    """要求 ISO 8601 且带时区；统一转成带偏移的 ISO 字符串。"""
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MetaValidationError(
            "meta.generated_at: 必须是 ISO 8601 日期时间（如 "
            "2026-10-21T18:00:00+08:00）"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MetaValidationError(
            "meta.generated_at: 必须带显式时区偏移（不接受无时区的本地时间）"
        )
    return parsed.isoformat()


def _validate_code_version(value: str) -> str:
    if value.strip() == "uncommitted":
        return "uncommitted"
    if _CODE_VERSION_PATTERN.match(value.strip()):
        return value.strip()
    raise MetaValidationError(
        "meta.code_version: 必须为 'uncommitted' 或 'git:<7~40 位 short-sha>'"
    )


def validate_meta(raw: Any) -> MetricMeta:
    """校验单个 meta 对象（不含外层 ``meta`` 包装）。

    - 只接受 §6.8 列出的字段，未知字段直接拒绝（防止契约漂移）；
    - ``sample_size`` 必须为正整数（bool 不算整数）；
    - ``generated_at`` 必须为带时区的 ISO 8601；
    - ``code_version`` 为 ``uncommitted`` 或 ``git:<7~40 位 sha>``；
    - ``data_kind`` 仅允许 ``offline`` / ``demo``。
    """
    if not isinstance(raw, Mapping):
        raise MetaValidationError("meta: 必须是 JSON 对象")
    unknown = sorted(set(raw) - set(REQUIRED_META_FIELDS))
    if unknown:
        raise MetaValidationError(f"meta: 未知字段 {unknown}")
    missing = [field for field in REQUIRED_META_FIELDS if field not in raw]
    if missing:
        raise MetaValidationError(f"meta: 缺少必需字段 {missing}")

    dataset = _require_non_empty_str(raw, "dataset")
    split = _require_non_empty_str(raw, "split")

    sample_size = raw["sample_size"]
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise MetaValidationError("meta.sample_size: 必须是正整数")
    if sample_size <= 0:
        raise MetaValidationError("meta.sample_size: 必须大于 0")

    generated_at_value = _require_non_empty_str(raw, "generated_at")
    generated_at = _validate_generated_at(generated_at_value)
    code_version = _validate_code_version(_require_non_empty_str(raw, "code_version"))
    model_versions = _require_mapping(raw, "model_versions")
    metric_def = _require_mapping(raw, "metric_def")
    data_kind = _require_non_empty_str(raw, "data_kind")
    if data_kind not in ALLOWED_DATA_KINDS:
        raise MetaValidationError(
            f"meta.data_kind: 只允许 {'/'.join(ALLOWED_DATA_KINDS)}，"
            f"实际为 {data_kind!r}"
        )

    return MetricMeta(
        dataset=dataset,
        split=split,
        sample_size=sample_size,
        generated_at=generated_at,
        code_version=code_version,
        model_versions=model_versions,
        metric_def=metric_def,
        data_kind=data_kind,
    )


def validate_document(document: Any) -> MetricMeta:
    """校验产物文件：要求顶层为对象且包含 ``meta``。"""
    if not isinstance(document, Mapping):
        raise MetaValidationError("产物: 顶层必须是 JSON 对象")
    if "meta" not in document:
        raise MetaValidationError("产物: 缺少顶层 meta 字段")
    return validate_meta(document["meta"])


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MetaValidationError(f"文件不存在: {path}") from exc
    except UnicodeDecodeError as exc:
        raise MetaValidationError(f"文件不是 UTF-8: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetaValidationError(
            f"JSON 解析失败（line {exc.lineno}, column {exc.colno}）: {exc.msg}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验离线指标产物中的 §6.8 meta 契约"
    )
    parser.add_argument("artifact", help="产物 JSON 文件路径")
    args = parser.parse_args(argv)

    path = Path(args.artifact)
    try:
        document = _load_json(path)
        meta = validate_document(document)
    except MetaValidationError as exc:
        print(f"META_ERROR: {exc}")
        return 2
    except Exception as exc:  # 文件/IO 等非契约错误
        print(f"META_ERROR: {type(exc).__name__}: {exc}")
        return 1

    print(
        "META_OK "
        f"data_kind={meta.data_kind} split={meta.split} "
        f"sample_size={meta.sample_size} code_version={meta.code_version} "
        f"generated_at={meta.generated_at}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
