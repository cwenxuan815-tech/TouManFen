"""阶段 2 网关配置加载与校验。

职责边界：
- 只读取 serving/config/workers.yaml 与 models.yaml；
- 类别白名单唯一来源为 00-data/data/class.txt；
- 路径基于本文件位置解析，不依赖当前工作目录；
- 不读取任何密钥；配置中出现敏感字段名直接拒绝；
- 错误信息只包含文件/字段/原因，不回显配置值。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError

# serving/gateway/settings.py -> serving/gateway -> serving -> TouManFenClassifier
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVING_DIR = PROJECT_ROOT / "serving"
DEFAULT_WORKERS_FILE = SERVING_DIR / "config" / "workers.yaml"
DEFAULT_MODELS_FILE = SERVING_DIR / "config" / "models.yaml"
DEFAULT_CLASSES_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"

REQUIRED_MODEL_IDS = frozenset({"rf", "fasttext", "bert", "llm", "distill"})
_FORBIDDEN_KEY_TOKENS = ("password", "secret", "apikey", "token", "authorization")


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
    workers_file: Path
    models_file: Path
    classes_file: Path


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

    default_model = os.environ.get("TMF_DEFAULT_MODEL") or models_doc.default_model
    if default_model not in models_doc.models:
        raise ConfigError(f"{models_path}: default_model '{default_model}' 不在 models 中")
    if not models_doc.models[default_model].enabled:
        raise ConfigError(f"{models_path}: default_model '{default_model}' 处于禁用状态")

    for model_id, model in models_doc.models.items():
        if model.worker not in workers_doc.workers:
            raise ConfigError(
                f"{models_path}:{model_id}.worker: 引用了不存在的 worker '{model.worker}'"
            )
        if model.enabled and not workers_doc.workers[model.worker].enabled:
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

    return Settings(
        workers=dict(workers_doc.workers),
        models=dict(models_doc.models),
        classes=classes,
        default_model=default_model,
        max_title_chars=max_title_chars,
        probe_timeout_ms=probe_timeout_ms,
        status_cache_ms=status_cache_ms,
        workers_file=workers_path,
        models_file=models_path,
        classes_file=classes_path,
    )
