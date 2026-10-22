"""随机森林预测服务（统一 PredictService 契约）。

模型在模块导入时加载一次；``predict(dict)`` 保留为向后兼容的旧接口，
新代码请使用 ``SERVICE.predict(title)`` 或 ``SERVICE.handle_payload(dict)``。

运行位置：本文件由 ``01-rf/api_flask_server.py`` 导入，进程工作目录为模块目录，
因此这里显式把工程根加入 ``sys.path``，保证 ``tmf_common`` 可导入且不依赖当前 shell 路径。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import jieba  # noqa: E402
import pickle  # noqa: E402

from config import Config  # noqa: E402
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402

from tmf_common.prediction import (  # noqa: E402
    ModelOutput,
    PredictService,
    SCORE_KIND_RAW_PROBA,
)

config = Config()

with open(config.rf_model_path, mode="rb") as f:
    _MODEL: RandomForestClassifier = pickle.load(f)

with open(config.tfidf_model_path, mode="rb") as f:
    _TFIDF: TfidfVectorizer = pickle.load(f)

with open(config.class_datapath, mode="r", encoding="UTF-8") as f:
    _CLASSES = [line.strip() for line in f if line.strip()]


class RfPredictService(PredictService):
    """TF-IDF + 随机森林。"""

    model_id = "rf"
    display_name = "随机森林"

    def __init__(self, classes, model, tfidf):
        super().__init__(classes)
        self._model = model
        self._tfidf = tfidf

    def infer(self, title: str) -> ModelOutput:
        # 分词 -> 取前 30 个词 -> 空格分隔 -> TF-IDF -> 随机森林
        words = " ".join(jieba.lcut(title)[:30])
        features = self._tfidf.transform([words])
        pred_index = int(self._model.predict(features)[0])
        scores = self._predict_proba_scores(features)
        return ModelOutput(
            pred_class=self.class_list[pred_index],
            scores=scores,
            score_kind=SCORE_KIND_RAW_PROBA if scores is not None else None,
        )

    def _predict_proba_scores(
        self, features
    ) -> tuple[float, ...] | None:
        """把 ``predict_proba`` 还原为与 ``class_list`` 索引对应的分数。

        RF 训练标签是 ``class.txt`` 的整数索引（0..9），但列顺序由
        ``model.classes_`` 决定；必须按标签映射，不能按 argmax 重算。
        无法一一对应时返回 ``None``（表示“无分数”），不编造替代概率。
        """
        probabilities = self._model.predict_proba(features)
        if len(probabilities) != 1:
            return None
        row = probabilities[0]
        labels = getattr(self._model, "classes_", None)
        if labels is None or len(labels) != len(row):
            return None
        mapped: dict[int, float] = {}
        for label, value in zip(labels, row):
            try:
                index = int(label)
                numeric = float(label)
            except (TypeError, ValueError):
                return None
            if isinstance(label, bool) or numeric != index:
                return None
            if not 0 <= index < len(self.class_list) or index in mapped:
                return None
            mapped[index] = float(value)
        if len(mapped) != len(self.class_list):
            return None
        return tuple(mapped[index] for index in range(len(self.class_list)))


SERVICE = RfPredictService(_CLASSES, _MODEL, _TFIDF)


def predict(news_data):
    """向后兼容：``{"title": ...} -> {"title": ..., "pred_class": ...}``。"""
    return SERVICE.handle_payload(news_data)


if __name__ == "__main__":
    print(SERVICE.predict("同步A股首秀：港股缩量回调"))
