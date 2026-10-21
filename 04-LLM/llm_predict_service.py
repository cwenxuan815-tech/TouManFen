# 大模型分类服务（阶段 4A：上游调用超时与有限重试）
#
# 相对上一版的改动：
#   1. 新增上游总时间预算 TMF_LLM_TOTAL_TIMEOUT_S（默认 12s，必须小于网关 llm timeout 15s）；
#   2. OpenAI 客户端禁用 SDK 自动重试（max_retries=0），每次请求使用剩余预算作为 timeout；
#   3. 输出格式重试最多一次，且必须检查总 deadline；预算不足则不再发起第二次请求；
#   4. 错误响应只返回稳定错误码/通用说明，不包含原始异常文本、供应商 URL、标题或密钥；
#   5. 日志只记录 provider/model/错误码/尝试次数/耗时，不记录标题或密钥。
#
# 接口：predict({"title": "..."}) -> {"title": "...", "pred_class": "sports"}
import logging
import os
import time

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, OpenAI

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_MODULE_DIR, ".env"))

CLASSES = ["finance", "realty", "stocks", "education", "science",
           "society", "politics", "sports", "game", "entertainment"]

_PROVIDERS = {
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "deepseek-chat"),
    "qwen": ("QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "qwen-plus"),
    "zhipu": ("ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL", "glm-4-flash"),
}

PROVIDER = os.environ.get("TMF_LLM_PROVIDER", "deepseek").lower()
_key_env, _base_env, _model_env, _default_model = _PROVIDERS.get(PROVIDER, _PROVIDERS["deepseek"])
API_KEY = os.getenv(_key_env)
BASE_URL = os.getenv(_base_env)
MODEL = os.getenv(_model_env, _default_model)

if not API_KEY:
    raise RuntimeError(f"环境变量 {_key_env} 为空，请在 04-LLM/.env 中配置（参考 .env.example）")

# 上游总时间预算。默认 12s，严格小于网关对 LLM worker 的 15s 超时。
DEFAULT_TOTAL_TIMEOUT_S = 12.0
MIN_TOTAL_TIMEOUT_S = 0.5
MAX_TOTAL_TIMEOUT_S = 14.0
MIN_ATTEMPT_SECONDS = 0.2


def _read_total_timeout() -> float:
    raw = os.environ.get("TMF_LLM_TOTAL_TIMEOUT_S", str(DEFAULT_TOTAL_TIMEOUT_S))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("环境变量 TMF_LLM_TOTAL_TIMEOUT_S 必须是数字") from exc
    if not MIN_TOTAL_TIMEOUT_S <= value <= MAX_TOTAL_TIMEOUT_S:
        raise RuntimeError(
            f"环境变量 TMF_LLM_TOTAL_TIMEOUT_S 必须在 "
            f"{MIN_TOTAL_TIMEOUT_S}~{MAX_TOTAL_TIMEOUT_S} 秒之间"
        )
    return value


TOTAL_TIMEOUT_S = _read_total_timeout()

# 禁用 SDK 隐式重试；每次请求的 timeout 由剩余预算控制。
try:
    llm_client = OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        timeout=TOTAL_TIMEOUT_S,
        max_retries=0,
    )
except Exception as exc:
    # 不输出 key、base_url 或原始异常文本；仅给出安全的本地初始化错误。
    raise RuntimeError("LLM 本地客户端初始化失败") from exc

SYSTEM_PROMPT = """你是一名中文新闻标题分类器。请把用户给出的新闻标题归到以下 10 个英文类别之一：
finance（金融）、realty（房地产）、stocks（股票/证券）、education（教育）、science（科技）、
society（社会）、politics（政治）、sports（体育）、game（游戏）、entertainment（娱乐）

规则：
1. 只输出一个类别名（英文小写），不要输出标点、解释、中文、编号或任何其他内容。
2. 判断依据只能是标题本身的语义；不确定时按标题的核心名词归类。
3. 注意易混类别：
   - 足球/篮球等实体运动 → sports；电子游戏/网游 → game。
   - 股价/大盘/券商/基金 → stocks；房价/楼市/开发商 → realty；宏观经济/银行/利率 → finance。
   - 科研/自然现象/新技术 → science；民生事件/事故/纠纷 → society。
4. 输出示例：sports
"""

_RETRY_PROMPT = ("只回复一个英文类别名（finance/realty/stocks/education/science/society/politics/sports/game/"
                 "entertainment），不要任何其它字符。")

_logger = logging.getLogger("tmf.llm")


class _LLMTimeout(RuntimeError):
    """总时间预算耗尽或上游读取超时。"""


class _LLMUpstreamError(RuntimeError):
    """上游连接/HTTP/API 错误。"""


def _call_llm(user_content, system_prompt, timeout_s):
    # with_options 复用底层 httpx 客户端；同时显式传 timeout 与 max_retries。
    client = llm_client.with_options(timeout=timeout_s, max_retries=0)
    return client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        stream=False,
        timeout=timeout_s,
    )


def _call_with_deadline(user_content, system_prompt, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= MIN_ATTEMPT_SECONDS:
        raise _LLMTimeout("总时间预算不足")
    try:
        response = _call_llm(user_content, system_prompt, remaining)
    except APITimeoutError as exc:
        raise _LLMTimeout("上游调用超时") from exc
    except APIError as exc:
        raise _LLMUpstreamError("上游 API 调用失败") from exc
    except Exception as exc:
        raise _LLMUpstreamError("上游调用异常") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise _LLMUpstreamError("上游响应结构异常") from exc
    return content


def _normalize(text):
    """把模型输出规整成 10 类之一；无法规整则返回 None"""
    if not text:
        return None
    t = text.strip().lower().strip("。.!！\"'`* \n\r\t")
    if t in CLASSES:
        return t
    for c in CLASSES:          # 处理 "sports." / "类别：sports" / 多候选等情况
        if c in t:
            return c
    return None


def _fail(news_data, code, message):
    news_data["error"] = f"{code}: {message}"
    news_data["pred_class"] = ""
    return news_data


def _log_failure(code, attempt, started, detail=""):
    _logger.warning(
        "llm_call_failed | code=%s attempt=%s elapsed_ms=%.1f provider=%s model=%s detail=%s",
        code,
        attempt,
        (time.monotonic() - started) * 1000,
        PROVIDER,
        MODEL,
        detail,
    )


def predict(news_data):
    """
    对单条新闻标题做分类
    :param news_data: {"title": "新闻标题"}
    :return: {"title": "...", "pred_class": "sports"}；异常时额外带 "error"
    """
    title = news_data.get("title", "")
    if not title:
        news_data["error"] = "缺少 title 字段"
        return news_data

    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT_S
    attempt = 0

    try:
        attempt = 1
        raw = _call_with_deadline(title, SYSTEM_PROMPT, deadline)
        pred = _normalize(raw)

        if pred is None:
            remaining = deadline - time.monotonic()
            if remaining <= MIN_ATTEMPT_SECONDS:
                _log_failure("LLM_TIMEOUT", attempt, started, "budget_exhausted_before_format_retry")
                return _fail(
                    news_data,
                    "LLM_TIMEOUT",
                    "总时间预算不足，未进行格式重试",
                )
            attempt = 2
            raw = _call_with_deadline(title, _RETRY_PROMPT, deadline)
            pred = _normalize(raw)

        if pred is None:
            _log_failure("LLM_INVALID_OUTPUT", attempt, started, "unparseable_output")
            return _fail(
                news_data,
                "LLM_INVALID_OUTPUT",
                "模型输出无法解析为 10 个合法类别",
            )

        news_data["pred_class"] = pred
        _logger.info(
            "llm_call_ok | attempt=%s elapsed_ms=%.1f provider=%s model=%s",
            attempt,
            (time.monotonic() - started) * 1000,
            PROVIDER,
            MODEL,
        )
        return news_data
    except _LLMTimeout as exc:
        _log_failure("LLM_TIMEOUT", attempt or 1, started, str(exc))
        return _fail(news_data, "LLM_TIMEOUT", "上游调用超过总时间预算")
    except _LLMUpstreamError as exc:
        _log_failure("LLM_UPSTREAM_ERROR", attempt or 1, started, str(exc))
        return _fail(news_data, "LLM_UPSTREAM_ERROR", "上游 LLM 调用失败")
    except Exception as exc:  # 本地 SDK/客户端异常也归为上游失败，不泄露原始文本
        _log_failure("LLM_UPSTREAM_ERROR", attempt or 1, started, type(exc).__name__)
        return _fail(news_data, "LLM_UPSTREAM_ERROR", "上游 LLM 调用失败")


if __name__ == "__main__":
    print(f"provider={PROVIDER} model={MODEL} total_timeout_s={TOTAL_TIMEOUT_S}")
    for t in ["化危为机 推动我国期市创新发展", "体验2D巅峰 倚天屠龙记十大创新概览", "2岁男童7楼坠下获救"]:
        print(predict({"title": t}))
