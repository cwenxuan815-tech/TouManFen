"""阶段 2 网关配置加载与校验。

职责边界：
- 只读取 serving/config/workers.yaml 与 models.yaml；
- 类别白名单唯一来源为 00-data/data/class.txt；
- 路径基于本文件位置解析，不依赖当前工作目录；
- 不读取任何密钥；配置中出现敏感字段名直接拒绝；
- 错误信息只包含文件/字段/原因，不回显配置值。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tmf_common.config import (
    NetworkConfigError,
    NetworkSettings,
    load_network_settings,
    resolve_worker_url,
)

from .errors import ConfigError

# serving/gateway/settings.py -> serving/gateway -> serving -> TouManFenClassifier
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVING_DIR = PROJECT_ROOT / "serving"
DEFAULT_WORKERS_FILE = SERVING_DIR / "config" / "workers.yaml"
DEFAULT_MODELS_FILE = SERVING_DIR / "config" / "models.yaml"
DEFAULT_CLASSES_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"

REQUIRED_MODEL_IDS = frozenset({"rf", "fasttext", "bert", "llm", "distill"})
_FORBIDDEN_KEY_TOKENS = ("password", "secret", "apikey", "token", "authorization")

#: B6：/compare 与 /compare/stream 共用的总截止默认值（工作清单 §6.6）。
#: 环境变量 ``TMF_COMPARE_TOTAL_TIMEOUT_MS`` 可覆盖。
DEFAULT_COMPARE_TOTAL_TIMEOUT_MS = 18000

#: B12：/compare 与 /compare/stream 的并发上限默认值（工作清单 §6.6：由 5 降为 3）。
#: 环境变量 ``TMF_COMPARE_CONCURRENCY`` 可覆盖；范围 1~10（当前最多 5 个模型，
#: 上限留出余量但拒绝 0/负数/超大值，避免把进程打爆）。
DEFAULT_COMPARE_CONCURRENCY = 3
COMPARE_CONCURRENCY_MIN = 1
COMPARE_CONCURRENCY_MAX = 10


class UniqueKeyLoader(yaml.SafeLoader):
    """拒绝重复键，避免后出现的值静默覆盖前值。"""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(
                f"YAML 重复键 '{key}'（line {key_node.start_mark.line + 1}）"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    predict_path: str = "/predict_api"
    health_path: str = "/healthz"
    ready_path: str = "/readyz"
    metadata_path: str = "/metadata"
    timeout_ms: int = Field(default=10000, ge=100, le=120000)
    enabled: bool = True
    probe: str = "http_ready"
    probe_timeout_ms: int | None = Field(default=None, ge=100, le=10000)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须形如 http(s)://host[:port]")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 不允许用户信息、查询串或片段")
        if parsed.path not in {"", "/"}:
            raise ValueError("base_url 不应包含路径，请使用 *_path 字段")
        return value.rstrip("/")

    @field_validator("predict_path", "health_path", "ready_path", "metadata_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("路径必须以 / 开头")
        return value

    @field_validator("probe")
    @classmethod
    def _validate_probe(cls, value: str) -> str:
        if value not in {"http_ready", "tcp"}:
            raise ValueError("probe 必须是 http_ready 或 tcp")
        return value


class WorkersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    probe_timeout_ms: int = Field(default=1000, ge=100, le=10000)
    status_cache_ms: int = Field(default=2000, ge=0, le=60000)
    workers: dict[str, WorkerConfig]


class ClassesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    expected_count: int = Field(default=10, ge=1, le=100)


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_title_chars: int = Field(default=200, ge=1, le=1000)


class RejectCalibrationConfig(BaseModel):
    """B3 软拒识阈值的来源元数据（必须如实描述“合成负样本 + 未校准”）。

    ``method`` 固定为 ``max_score+entropy``；``calibrated`` 本期必须为 ``false``
    （L2 校准已移出本期，见工作清单 §4.1）。
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["synthetic_negatives"]
    calibrated: bool = False
    method: Literal["max_score+entropy"]
    dataset: str
    positive_sample_size: int = Field(ge=1)
    negative_sample_size: int = Field(ge=1)
    seed: int
    derived_for_model: str
    roc_artifact: str
    threshold_selection_artifact: str
    scope_note: str

    @field_validator("dataset", "roc_artifact", "threshold_selection_artifact", "scope_note")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空")
        return value

    @field_validator("calibrated")
    @classmethod
    def _validate_calibrated(cls, value: bool) -> bool:
        # 本期不做 L2 校准（B2 移出本期）；若配置声称已校准，必须同步文档与流程。
        if value:
            raise ValueError("本期分数未校准，calibrated 必须为 false")
        return value


class RejectConfig(BaseModel):
    """B3 软拒识配置：有分数时按 max_score + entropy 做软拒识（不丢 pred_class）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_score_threshold: float = Field(gt=0.0, le=1.0)
    # 归一化 10 类分数的 Shannon entropy（base=e）上界约为 ln(10)；上界放宽到
    # ln(100) 以支持最多 100 类，加载时再按实际类别数校验有效性。
    entropy_threshold: float = Field(ge=0.0, le=math.log(100.0))
    calibration: RejectCalibrationConfig


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str
    display_name: str
    model_version: str | None = None
    enabled: bool = True
    compare_by_default: bool = True

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name 不能为空")
        return value


class ModelsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    default_model: str
    api: ApiConfig = Field(default_factory=ApiConfig)
    #: B3 软拒识配置；缺失即配置非法（避免“静默关闭拒识”）。
    reject: RejectConfig
    classes: ClassesConfig
    models: dict[str, ModelConfig]


@dataclass(frozen=True)
class Settings:
    workers: dict[str, WorkerConfig]
    models: dict[str, ModelConfig]
    classes: tuple[str, ...]
    default_model: str
    max_title_chars: int
    probe_timeout_ms: int
    status_cache_ms: int
    #: B6 SSE 渐进式对比的总截止（毫秒，默认 18000，§6.6）。
    compare_total_timeout_ms: int
    #: B12 /compare 与 /compare/stream 的并发上限（默认 3，§6.6）。
    compare_concurrency: int
    reject: RejectConfig
    workers_file: Path
    models_file: Path
    classes_file: Path
    network: NetworkSettings


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=UniqueKeyLoader)
    except ConfigError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"（line {mark.line + 1}, column {mark.column + 1}）" if mark else ""
        problem = getattr(exc, "problem", "YAML 解析失败")
        raise ConfigError(f"{path}{location}: {problem}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return data


def _scan_forbidden_keys(node: Any, path: str) -> None:
    if isinstance(node, dict):
        for raw_key, value in node.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(token in normalized for token in _FORBIDDEN_KEY_TOKENS):
                raise ConfigError(
                    f"{path}: 禁止的敏感字段名 '{key}'（网关配置不得包含密钥）"
                )
            _scan_forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_forbidden_keys(value, f"{path}[{index}]")


def _raise_validation_error(path: Path, exc: ValidationError) -> None:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "校验失败")
        parts.append(f"{path}:{location}: {message}")
    raise ConfigError("; ".join(parts)) from exc


def _validate_ids(actual: set[str], path: Path, kind: str) -> None:
    if actual != set(REQUIRED_MODEL_IDS):
        missing = sorted(set(REQUIRED_MODEL_IDS) - actual)
        extra = sorted(actual - set(REQUIRED_MODEL_IDS))
        raise ConfigError(
            f"{path}: {kind}集合必须为 {sorted(REQUIRED_MODEL_IDS)}；"
            f"缺失={missing}，多余={extra}"
        )


def _load_classes(path: Path, expected_count: int) -> tuple[str, ...]:
    if not path.is_file():
        raise ConfigError(f"类别文件不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"类别文件不是 UTF-8: {path}") from exc
    lines = text.splitlines()
    if any(not line.strip() for line in lines):
        raise ConfigError(f"类别文件包含空行: {path}")
    classes = tuple(line.strip() for line in lines)
    if len(classes) != expected_count:
        raise ConfigError(
            f"{path}: 类别数量错误，期望 {expected_count}，实际 {len(classes)}"
        )
    if len(set(classes)) != len(classes):
        raise ConfigError(f"{path}: 类别存在重复项")
    if any(not re.match(r"^[a-z][a-z0-9_]*$", name) for name in classes):
        raise ConfigError(f"{path}: 类别 ID 必须小写字母开头且仅含小写字母/数字/下划线")
    return classes


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 必须在 {minimum}~{maximum} 之间")
    return value


def _env_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    *,
    exclusive_min: bool = False,
) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字") from exc
    if not math.isfinite(value):
        raise ConfigError(f"环境变量 {name} 必须是有限数字")
    low_ok = value > minimum if exclusive_min else value >= minimum
    if not low_ok or value > maximum:
        compare = "大于" if exclusive_min else "不小于"
        raise ConfigError(
            f"环境变量 {name} 必须{compare} {minimum} 且不大于 {maximum}"
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"环境变量 {name} 必须是 true/false（或 1/0）")


def _resolve_reject_config(
    models_path: Path,
    reject: RejectConfig,
    classes: tuple[str, ...],
    model_ids: set[str],
) -> RejectConfig:
    """校验 reject 配置与模型/类别数的一致性，并应用环境变量覆盖。

    环境变量（全部可选，未设置时用 models.yaml 的值）：
    - ``TMF_REJECT_ENABLED``：true/false
    - ``TMF_REJECT_MAX_SCORE_THRESHOLD``：(0, 1]
    - ``TMF_REJECT_ENTROPY_THRESHOLD``：[0, ln(类别数))
    """
    if reject.calibration.derived_for_model not in model_ids:
        raise ConfigError(
            f"{models_path}:reject.calibration.derived_for_model: "
            f"'{reject.calibration.derived_for_model}' 不在 models 中"
        )

    enabled = _env_bool("TMF_REJECT_ENABLED", reject.enabled)
    max_score_threshold = _env_float(
        "TMF_REJECT_MAX_SCORE_THRESHOLD",
        reject.max_score_threshold,
        0.0,
        1.0,
        exclusive_min=True,
    )
    entropy_limit = math.log(len(classes))
    entropy_threshold = _env_float(
        "TMF_REJECT_ENTROPY_THRESHOLD",
        reject.entropy_threshold,
        0.0,
        entropy_limit,
    )
    if entropy_threshold >= entropy_limit:
        raise ConfigError(
            f"{models_path}:reject.entropy_threshold 必须小于 ln(类别数)"
            f"={entropy_limit:.4f}，否则该条件永不触发"
        )

    return reject.model_copy(
        update={
            "enabled": enabled,
            "max_score_threshold": max_score_threshold,
            "entropy_threshold": entropy_threshold,
        }
    )


def load_settings(
    workers_file: str | Path | None = None,
    models_file: str | Path | None = None,
    class_file: str | Path | None = None,
) -> Settings:
    workers_path = _resolve_path(
        workers_file or os.environ.get("TMF_WORKERS_FILE") or DEFAULT_WORKERS_FILE
    )
    models_path = _resolve_path(
        models_file or os.environ.get("TMF_MODELS_FILE") or DEFAULT_MODELS_FILE
    )

    raw_workers = _read_yaml(workers_path)
    raw_models = _read_yaml(models_path)
    _scan_forbidden_keys(raw_workers, str(workers_path))
    _scan_forbidden_keys(raw_models, str(models_path))

    try:
        workers_doc = WorkersFile.model_validate(raw_workers)
    except ValidationError as exc:
        _raise_validation_error(workers_path, exc)
    try:
        models_doc = ModelsFile.model_validate(raw_models)
    except ValidationError as exc:
        _raise_validation_error(models_path, exc)

    _validate_ids(set(workers_doc.workers), workers_path, "worker")
    _validate_ids(set(models_doc.models), models_path, "model")

    # 环境变量覆盖 worker 地址（源码/配置不写死局域网 IP）：
    #   TMF_WORKER_HOST      -> 替换所有 worker base_url 的主机部分
    #   TMF_WORKER_URL_<ID>  -> 覆盖单个 worker 的完整 base_url
    workers: dict[str, WorkerConfig] = {}
    for worker_id, worker_cfg in workers_doc.workers.items():
        try:
            base_url = resolve_worker_url(worker_cfg.base_url, worker_id)
        except NetworkConfigError as exc:
            raise ConfigError(f"{workers_path}:{worker_id}.base_url: {exc}") from exc
        workers[worker_id] = (
            worker_cfg
            if base_url == worker_cfg.base_url
            else worker_cfg.model_copy(update={"base_url": base_url})
        )

    default_model = os.environ.get("TMF_DEFAULT_MODEL") or models_doc.default_model
    if default_model not in models_doc.models:
        raise ConfigError(f"{models_path}: default_model '{default_model}' 不在 models 中")
    if not models_doc.models[default_model].enabled:
        raise ConfigError(f"{models_path}: default_model '{default_model}' 处于禁用状态")

    for model_id, model in models_doc.models.items():
        if model.worker not in workers:
            raise ConfigError(
                f"{models_path}:{model_id}.worker: 引用了不存在的 worker '{model.worker}'"
            )
        if model.enabled and not workers[model.worker].enabled:
            raise ConfigError(
                f"{models_path}:{model_id}: 模型启用但其 worker '{model.worker}' 被禁用"
            )

    classes_path = _resolve_path(
        class_file or os.environ.get("TMF_CLASS_FILE") or models_doc.classes.file
    )
    classes = _load_classes(classes_path, models_doc.classes.expected_count)

    probe_timeout_ms = _env_int(
        "TMF_PROBE_TIMEOUT_MS", workers_doc.probe_timeout_ms, 100, 10000
    )
    status_cache_ms = _env_int(
        "TMF_STATUS_CACHE_MS", workers_doc.status_cache_ms, 0, 60000
    )
    max_title_chars = _env_int(
        "TMF_MAX_TITLE_CHARS", models_doc.api.max_title_chars, 1, 1000
    )
    # B6：/compare 渐进式 SSE 的总截止（只影响网关编排，不中止已开始的 worker）。
    compare_total_timeout_ms = _env_int(
        "TMF_COMPARE_TOTAL_TIMEOUT_MS",
        DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
        100,
        300000,
    )
    # B12：/compare 与 /compare/stream 的并发上限（默认 3，范围 1~10）。
    compare_concurrency = _env_int(
        "TMF_COMPARE_CONCURRENCY",
        DEFAULT_COMPARE_CONCURRENCY,
        COMPARE_CONCURRENCY_MIN,
        COMPARE_CONCURRENCY_MAX,
    )

    # B3 软拒识：严格校验来源元数据（derived_for_model/类别数），并支持环境变量覆盖。
    reject = _resolve_reject_config(
        models_path,
        models_doc.reject,
        classes,
        set(models_doc.models),
    )

    try:
        network = load_network_settings()
    except NetworkConfigError as exc:
        raise ConfigError(str(exc)) from exc

    return Settings(
        workers=workers,
        models=dict(models_doc.models),
        classes=classes,
        default_model=default_model,
        max_title_chars=max_title_chars,
        probe_timeout_ms=probe_timeout_ms,
        status_cache_ms=status_cache_ms,
        compare_total_timeout_ms=compare_total_timeout_ms,
        compare_concurrency=compare_concurrency,
        reject=reject,
        workers_file=workers_path,
        models_file=models_path,
        classes_file=classes_path,
        network=network,
    )
