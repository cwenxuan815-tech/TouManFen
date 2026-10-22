"""BERT 预测服务（统一 PredictService 契约）。

保留原实现的关键细节：
- ``bert-base-chinese`` + 自定义分类头（``bert_model.BertClassifierModel``）；
- ``tokenizer.batch_encode_plus``（transformers<5）；
- ``torch.load`` 加载 ``bert.pkl``，CPU 推理；
- tokenizer/张量结构不做通用化改写，避免破坏已训练权重。

保留 ``predict(dict)`` 作为向后兼容入口。
"""

import sys
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import torch  # noqa: E402
from transformers import BertTokenizer  # noqa: E402

from bert_model import BertClassifierModel  # noqa: E402
from config import Config  # noqa: E402

from tmf_common.prediction import (  # noqa: E402
    ModelOutput,
    PredictService,
    SCORE_KIND_RAW_SOFTMAX,
)

config = Config()

# B12：推理线程数必须在模型加载前生效（默认 4，TMF_TORCH_THREADS 可覆盖）。
torch.set_num_threads(config.torch_threads)

#: B12：进程内推理串行锁。单个 worker 进程内一次只允许一个 infer，
#: 避免同一进程内并发请求互相抢占 torch 线程；不同 worker 是不同进程，互不影响。
_INFER_LOCK = threading.Lock()


class BertPredictService(PredictService):
    """BERT 全量微调分类器。"""

    model_id = "bert"
    display_name = "BERT"

    def __init__(self, classes, model, tokenizer, device, max_length):
        super().__init__(classes)
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._max_length = max_length

    def infer(self, title: str) -> ModelOutput:
        # B12：锁内只做 tokenize + forward + 取分数，不做 I/O/日志。
        with _INFER_LOCK:
            tokens = self._tokenizer.batch_encode_plus(
                [title],
                padding="max_length",
                truncation=True,
                max_length=self._max_length,
            )
            input_ids = torch.tensor(tokens.input_ids).to(self._device)
            attention_mask = torch.tensor(tokens.attention_mask).to(self._device)

            with torch.no_grad():
                output = self._model(input_ids, attention_mask)
                pred_index = torch.argmax(output, dim=-1).item()
                # 分数来自同一 logits 向量的 softmax（未校准），不是按 argmax 重算。
                probabilities = torch.softmax(output, dim=-1)[0]
            scores = None
            if probabilities.shape[-1] == len(self.class_list):
                scores = tuple(float(value) for value in probabilities.tolist())
            return ModelOutput(
                pred_class=self.class_list[pred_index],
                scores=scores,
                score_kind=SCORE_KIND_RAW_SOFTMAX if scores is not None else None,
            )


def _load_model():
    model = BertClassifierModel().to(config.device)
    model.load_state_dict(torch.load(config.save_model))
    tokenizer = BertTokenizer.from_pretrained(config.bert_path)
    model.eval()
    return model, tokenizer


_MODEL, _TOKENIZER = _load_model()

SERVICE = BertPredictService(
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
