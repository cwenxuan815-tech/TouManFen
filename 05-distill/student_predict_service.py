"""蒸馏 BiLSTM 学生模型预测服务（统一 PredictService 契约）。

兼容 v1（embed 128）与 v2/v2b（embed 256）checkpoint：从 state_dict 自动推断
embedding/hidden/层数，无需改代码即可切换模型。默认加载
``config.student_save_model``（当前为 ``student_v2b.pkl``）。

保留 ``predict(dict)`` 作为向后兼容入口。
"""

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import torch  # noqa: E402
from transformers import BertTokenizer  # noqa: E402

from config import Config  # noqa: E402
from student_bilstm_v2 import BiLSTMStudentV2  # noqa: E402

from tmf_common.prediction import PredictService  # noqa: E402

config = Config()


class StudentPredictService(PredictService):
    """蒸馏 BiLSTM 学生模型。"""

    model_id = "distill"
    display_name = "蒸馏 BiLSTM"

    def __init__(self, classes, model, tokenizer, device, max_length):
        super().__init__(classes)
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._max_length = max_length

    def infer(self, title: str) -> str:
        encoded = self._tokenizer(
            [title],
            padding="max_length",
            truncation=True,
            max_length=self._max_length,
            return_tensors="pt",
        )
        input_ids = encoded["input_ids"].to(self._device)
        attention_mask = encoded["attention_mask"].to(self._device)

        with torch.no_grad():
            output = self._model(input_ids, attention_mask)
            pred_index = torch.argmax(output, dim=-1).item()
        return self.class_list[pred_index]


def _load_model():
    state = torch.load(config.student_save_model, map_location="cpu")
    embed_dim = state["embedding.weight"].shape[1]
    vocab_size = state["embedding.weight"].shape[0]
    hidden = state["lstm.weight_ih_l0"].shape[0] // 4
    layers = max(
        int(m.group(1))
        for key in state
        if (m := re.search(r"^lstm\.weight_ih_l(\d+)$", key))
    ) + 1

    model = BiLSTMStudentV2(
        vocab_size=vocab_size,
        class_len=config.classname_len,
        embedding_dim=embed_dim,
        hidden_size=hidden,
        num_layers=layers,
    )
    model.load_state_dict(state)
    model.to(config.device)
    model.eval()
    print(
        f"[student_predict_service] 已加载 {config.student_save_model} | "
        f"embed={embed_dim} hidden={hidden} layers={layers} "
        f"参数={sum(p.numel() for p in model.parameters()) / 1e6:.2f}M"
    )
    return model, BertTokenizer.from_pretrained(config.bert_path)


_MODEL, _TOKENIZER = _load_model()

SERVICE = StudentPredictService(
    config.classname_list,
    _MODEL,
    _TOKENIZER,
    config.device,
    config.max_length,
)


def predict(news_data):
    """向后兼容：``{"title": ...} -> {"title": ..., "pred_class": ...}``。"""
    return SERVICE.handle_payload(news_data)


if __name__ == "__main__":
    print(SERVICE.predict("体验2D巅峰 倚天屠龙记十大创新概览"))
    print(predict({"wrong": "x"}))
