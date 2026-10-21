# 蒸馏学生模型预测服务
#
# 说明：兼容 v1（student_bilstm_model.py, embed 128）与 v2（student_bilstm_v2.py, embed 256）
#       两种 checkpoint —— 从 state_dict 自动推断 embedding/hidden/层数，无需改代码即可切换模型。
#       默认加载 config.student_save_model（当前为 student_v2.pkl）。
from config import Config
from student_bilstm_v2 import BiLSTMStudentV2
from transformers import BertTokenizer
import re

import torch

# 1- 加载训练好的模型（自动推断结构）
config = Config()

_state = torch.load(config.student_save_model, map_location="cpu")
_embed_dim = _state["embedding.weight"].shape[1]
_vocab_size = _state["embedding.weight"].shape[0]
_hidden = _state["lstm.weight_ih_l0"].shape[0] // 4
_layers = max(int(m.group(1)) for k in _state
              if (m := re.search(r"^lstm\.weight_ih_l(\d+)$", k))) + 1

model = BiLSTMStudentV2(
    vocab_size=_vocab_size,
    class_len=config.classname_len,
    embedding_dim=_embed_dim,
    hidden_size=_hidden,
    num_layers=_layers,
)
model.load_state_dict(_state)
model.to(config.device)
model.eval()

tokenizer = BertTokenizer.from_pretrained(config.bert_path)
print(f"[student_predict_service] 已加载 {config.student_save_model} | "
      f"embed={_embed_dim} hidden={_hidden} layers={_layers} "
      f"参数={sum(p.numel() for p in model.parameters())/1e6:.2f}M")


# 2- 预测函数
def predict(news_data):
    # 入参兜底：缺 title 时返回结构化错误（而不是抛 KeyError 变成 500）
    if not isinstance(news_data, dict) or "title" not in news_data:
        news_data = dict(news_data) if isinstance(news_data, dict) else {}
        news_data["error"] = "传递的参数中没有title字段"
        return news_data

    title = news_data["title"]
    # 注意：这里用 tokenizer(...) 而不是 batch_encode_plus，兼容 transformers 4.x/5.x
    enc = tokenizer([title], padding="max_length", truncation=True,
                    max_length=config.max_length, return_tensors="pt")
    input_ids = enc["input_ids"].to(config.device)
    attention_mask = enc["attention_mask"].to(config.device)

    with torch.no_grad():
        pred_output = model(input_ids, attention_mask)
        pred_index = torch.argmax(pred_output, dim=-1).item()

    news_data["pred_class"] = config.classname_list[pred_index]
    return news_data


if __name__ == '__main__':
    print(predict({"title": "体验2D巅峰 倚天屠龙记十大创新概览"}))
    print(predict({"wrong": "x"}))
