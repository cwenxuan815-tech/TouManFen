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

from tmf_common.prediction import (  # noqa: E402
    ModelOutput,
    PredictService,
    SCORE_KIND_RAW_FASTTEXT,
    SCORE_MAX,
    SCORE_MIN,
)

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

    def infer(self, title: str) -> ModelOutput:
        words = " ".join(jieba.lcut(title))
        # 请求全部类别，才能得到与 class_list 对齐的完整分数向量。
        labels, probabilities = self._model.predict(words, k=len(self.class_list))
        pred_class = labels[0].replace("__label__", "")
        scores = self._scores_from_predictions(labels, probabilities)
        return ModelOutput(
            pred_class=pred_class,
            scores=scores,
            score_kind=SCORE_KIND_RAW_FASTTEXT if scores is not None else None,
        )

    def _scores_from_predictions(
        self, labels, probabilities
    ) -> tuple[float, ...] | None:
        """按 fastText 返回的标签名（``__label__xxx``）映射到 class_list。

        标签与概率必须逐项配对且覆盖全部类别；不完整/重复/白名单外时
        返回 ``None``（表示“无分数”），不编造替代概率。
        """
        if len(labels) != len(probabilities) or len(labels) != len(self.class_list):
            return None
        mapped: dict[str, float] = {}
        for label, probability in zip(labels, probabilities):
            class_id = label.replace("__label__", "")
            if class_id not in self.class_list or class_id in mapped:
                return None
            # fastText 的 float32 归一化可能让最大值略大于 1（如 1.00000894），
            # 按契约 [0, 1] 截断；只做数值收敛，不改变来源与排序。
            mapped[class_id] = min(SCORE_MAX, max(SCORE_MIN, float(probability)))
        if set(mapped) != set(self.class_list):
            return None
        return tuple(mapped[class_id] for class_id in self.class_list)


SERVICE = FasttextPredictService(_CLASSES, _MODEL)


def predict(news_data):
    """向后兼容：``{"title": ...} -> {"title": ..., "pred_class": ...}``。"""
    return SERVICE.handle_payload(news_data)


if __name__ == "__main__":
    print(SERVICE.predict("体验2D巅峰 倚天屠龙记十大创新概览"))
