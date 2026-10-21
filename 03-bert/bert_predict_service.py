from config import Config
from bert_model import BertClassifierModel
from transformers import BertTokenizer
import torch

# 1- 加载训练好的模型
config = Config()

model = BertClassifierModel().to(config.device)
# 注意：不要用变量接受返回结果
model.load_state_dict(torch.load(config.save_model))
tokenizer = BertTokenizer.from_pretrained(config.bert_path)

model.eval()

# 2- 预测函数
def predict(news_data):
    # 数据预处理
    # 入参兜底：缺 title 时返回结构化错误（而不是抛 KeyError 变成 500）
    if not isinstance(news_data, dict) or "title" not in news_data:
        news_data = dict(news_data) if isinstance(news_data, dict) else {}
        news_data["error"] = "传递的参数中没有title字段"
        return news_data
    title = news_data["title"]
    # 注意：这里用的是batch_encode_plus，而title只有1条新闻，因此下面的参数需要使用中括号包起来
# 注意：batch_encode_plus 在 transformers 5.x 已移除，本工程锁定 transformers<5（见 requirements.txt）
    data_tensor = tokenizer.batch_encode_plus(
        [title],
        padding="max_length",
        truncation=True,
        max_length=config.max_length
    )

    input_ids = torch.tensor(data_tensor.input_ids).to(config.device)
    attention_mask = torch.tensor(data_tensor.attention_mask).to(config.device)

    # 模型预测
    with torch.no_grad():
        # 前向传播：预测
        pred_output = model(input_ids,attention_mask)
        # 获得概率最高的分类索引
        pred_index = torch.argmax(pred_output,dim=-1).item()
        # 索引转成类别名称
        pred_class_name = config.classname_list[pred_index]

    # 返回结果
    news_data["pred_class"] = pred_class_name

    return news_data

if __name__ == '__main__':
    print(predict({"title": "体验2D巅峰 倚天屠龙记十大创新概览"}))

