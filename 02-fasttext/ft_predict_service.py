"""fastText 预测服务（统一 PredictService 契约）。

词级 auto 模型（``word_auto_train.pkl``）在模块导入时加载一次。
保留 ``predict(dict)`` 作为向后兼容入口。

注意：fastText 词级模型要求先 jieba 分词，否则效果明显下降
（见 docs/实测指标.md 的分词一致性验证）。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import fasttext  # noqa: E402
import jieba  # noqa: E402

from config import Config  # noqa: E402

from tmf_common.prediction import PredictService  # noqa: E402

config = Config()

# 词级模型效果最好，服务端保持加载 word_auto。
_MODEL = fasttext.load_model(config.model_word_auto_train)

with open(config.class_datapath, mode="r", encoding="UTF-8") as f:
    _CLASSES = [line.strip() for line in f if line.strip()]


class FasttextPredictService(PredictService):
    """jieba 分词 + fastText 词级监督模型。"""

    model_id = "fasttext"
    display_name = "fastText"

    def __init__(self, classes, model):
        super().__init__(classes)
        self._model = model

    def infer(self, title: str) -> str:
        words = " ".join(jieba.lcut(title))
        labels, _probs = self._model.predict(words)
        return labels[0].replace("__label__", "")


SERVICE = FasttextPredictService(_CLASSES, _MODEL)


def predict(news_data):
    """向后兼容：``{"title": ...} -> {"title": ..., "pred_class": ...}``。"""
    return SERVICE.handle_payload(news_data)


if __name__ == "__main__":
    print(SERVICE.predict("体验2D巅峰 倚天屠龙记十大创新概览"))
