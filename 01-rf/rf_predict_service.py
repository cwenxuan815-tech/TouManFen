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

from tmf_common.prediction import PredictService  # noqa: E402

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

    def infer(self, title: str) -> str:
        # 分词 -> 取前 30 个词 -> 空格分隔 -> TF-IDF -> 随机森林
        words = " ".join(jieba.lcut(title)[:30])
        features = self._tfidf.transform([words])
        pred_index = int(self._model.predict(features)[0])
        return self.class_list[pred_index]


SERVICE = RfPredictService(_CLASSES, _MODEL, _TFIDF)


def predict(news_data):
    """向后兼容：``{"title": ...} -> {"title": ..., "pred_class": ...}``。"""
    return SERVICE.handle_payload(news_data)


if __name__ == "__main__":
    print(SERVICE.predict("同步A股首秀：港股缩量回调"))
