"""B8 离线基准（PK 榜）只读 API：``GET /api/v1/bench/pk``。

职责边界：

* 只读 ``artifacts/bench/`` 下**最新**的 ``pk_*.json``（按 mtime），
  不接受客户端传入任何路径/文件名，因此无法读取 bench 目录之外的任意文件；
* 返回 ``request_id`` + 工程根相对 ``artifact_path`` + 产物完整内容；
* 产物缺失 → 404 ``BENCH_ARTIFACT_NOT_FOUND``；产物不可读/结构非法 →
  503 ``BENCH_ARTIFACT_INVALID``（均使用网关统一错误体，不泄露堆栈）；
* 不做缓存/不写文件/不触发推理：离线产物由 ``tools/bench_signature.py``
  一次生成，API 只负责展示（B8 与 B13 共用同一产物）。

测试/部署可通过环境变量 ``TMF_BENCH_DIR`` 覆盖 bench 目录（仅服务端运维
可设置，客户端不可控），用于 verify_bench 验证「缺失产物」分支。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Request

from ..errors import (
    ERROR_BENCH_ARTIFACT_INVALID,
    ERROR_BENCH_NOT_FOUND,
    ApiError,
)
from ..settings import PROJECT_ROOT
from .bench_schemas import BenchPkResponse

router = APIRouter(prefix="/api/v1", tags=["bench"])

DEFAULT_BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"

#: 只认日期/时间后缀的产物文件名，拒绝任意文件（例如 snapshot.json、../x）。
ARTIFACT_FILENAME_PATTERN = re.compile(r"^pk_[0-9A-Za-z_-]+\.json$")

#: 产物最小结构（完整 §6.8 契约由 tools/validate_meta.py 在离线侧严格校验）。
REQUIRED_TOP_LEVEL_FIELDS = (
    "meta",
    "dataset",
    "models",
    "derived",
    "missing_models",
)
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


def bench_dir() -> Path:
    """当前 bench 目录（默认 ``artifacts/bench``，可被运维环境变量覆盖）。"""
    raw = os.environ.get("TMF_BENCH_DIR")
    if not raw:
        return DEFAULT_BENCH_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def display_path(path: Path) -> str:
    """工程根相对路径（用于响应/错误 details，不暴露绝对路径）。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def latest_artifact(directory: Path) -> Path | None:
    """返回目录内最新的 ``pk_*.json``；不存在/不可读时返回 None。"""
    try:
        if not directory.is_dir():
            return None
        resolved_dir = directory.resolve()
        best: Path | None = None
        best_mtime = float("-inf")
        for candidate in directory.iterdir():
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not ARTIFACT_FILENAME_PATTERN.match(candidate.name):
                continue
            try:
                resolved = candidate.resolve()
                # 双重保险：解析后仍必须位于 bench 目录内。
                if resolved.parent != resolved_dir:
                    continue
                mtime = resolved.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best = candidate
                best_mtime = mtime
        return best
    except OSError:
        return None


def _require_non_empty_string(meta: dict, field: str) -> None:
    value = meta.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"meta.{field} 必须是非空字符串")


def validate_artifact_structure(document: object) -> None:
    """网关侧最小结构校验（不重复 B13 的完整契约校验）。"""
    if not isinstance(document, dict):
        raise ValueError("产物顶层必须是 JSON 对象")
    missing = [field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in document]
    if missing:
        raise ValueError(f"产物缺少字段 {missing}")
    meta = document["meta"]
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是 JSON 对象")
    missing_meta = [field for field in REQUIRED_META_FIELDS if field not in meta]
    if missing_meta:
        raise ValueError(f"meta 缺少字段 {missing_meta}")
    for field in ("dataset", "split", "generated_at", "code_version", "data_kind"):
        _require_non_empty_string(meta, field)
    sample_size = meta.get("sample_size")
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("meta.sample_size 必须是正整数")
    if not isinstance(document["models"], list) or not document["models"]:
        raise ValueError("models 必须是非空数组")
    if not isinstance(document["derived"], list):
        raise ValueError("derived 必须是数组")
    if not isinstance(document["missing_models"], list):
        raise ValueError("missing_models 必须是数组")


@router.get(
    "/bench/pk",
    response_model=BenchPkResponse,
    summary="离线基准最新产物（5 模型 + 集成 + oracle）",
)
async def bench_pk(request: Request) -> BenchPkResponse:
    """只读返回最新离线基准产物；不缓存、不推理、不暴露任意路径。"""
    directory = bench_dir()
    artifact = latest_artifact(directory)
    if artifact is None:
        raise ApiError(
            404,
            ERROR_BENCH_NOT_FOUND,
            "离线基准产物不存在，请先运行 tools/bench_signature.py",
            {"bench_dir": display_path(directory)},
        )

    try:
        text = artifact.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(
            503,
            ERROR_BENCH_ARTIFACT_INVALID,
            "离线基准产物不可读或不是合法 JSON",
            {"artifact_name": artifact.name},
        ) from None

    try:
        validate_artifact_structure(document)
    except ValueError as exc:
        raise ApiError(
            503,
            ERROR_BENCH_ARTIFACT_INVALID,
            "离线基准产物结构不合法",
            {"artifact_name": artifact.name, "reason": str(exc)},
        ) from None

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    return BenchPkResponse(
        request_id=request_id,
        artifact_path=display_path(artifact),
        artifact=document,
    )
