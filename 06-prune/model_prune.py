# 模型剪枝：最核心代码
# 需求：BERT 全局非结构化剪枝：对所有 encoder 层注意力权重剪枝 30%，L1 范数
from data_preprocessing import build_dataloader
from model_eval import eval_model
from config import Config
from bert_model import BertClassifierModel
import torch
from torch.nn.utils import prune    # 模型剪枝方法

if __name__ == '__main__':
    config = Config()
    dev_dataloader = build_dataloader(config.dev_datapath,shuffle=False)
    num_layers = 12 # bert-base-chinese中编码器层有12层

    # 1- 剪枝前
    # 1.1- 加载训练好的模型
    model = BertClassifierModel().to(device=config.device)
    model.load_state_dict(torch.load(config.before_prune_path))
    # 1.2- 验证模型
    f1score, accuracy, precision, recall = eval_model(model)
    print(f"剪枝前，f1score={f1score}，accuracy={accuracy}，precision={precision}，recall={recall}")

    # 2- 【理解】剪枝中
    # 2.1- 规定对Bert模型中什么层的什么参数进行剪枝
    """
        代码解释：
            1- before_prune_model.bert_model.encoder.layer[layer_index].attention.self：
                对模型中 12个编码器层 中的 多头自注意力子层 进行剪枝
            2- multi_self_atten.query、key、value 多头自注意力子层 中的 q、k、v 的 权重w 进行剪枝
    """
    parameters_to_prune = []
    for layer_index in range(num_layers):
        multi_self_atten = model.bert_model.encoder.layer[layer_index].attention.self
        parameters_to_prune.extend([
            (multi_self_atten.query,"weight"),
            (multi_self_atten.key,"weight"),
            (multi_self_atten.value,"weight")
        ])

    # 2.2- 进行全局非结构化剪枝
    """
        参数解释：
            parameters：剪枝范围。也就是规定对Bert模型中什么层的什么参数进行剪枝
            pruning_method：剪枝方式
            amount：剪枝的参数情况。有两种类的参数值，如下
                整数：表示具体对多少个参数剪枝
                小数：表示对Bert模型中多大比例的参数进行剪枝。推荐
    """
    prune.global_unstructured(
        parameters=parameters_to_prune,
        pruning_method=prune.L1Unstructured,
        amount=0.3
    )

    # 2.3- 固化剪枝后的模型：将剪枝后的权重持久化存储到模型结构中
    for param_name,param_type in parameters_to_prune:
        prune.remove(param_name,param_type)

    # 3- 剪枝后
    # 3.1- 验证模型
    f1score, accuracy, precision, recall = eval_model(model)
    print(f"剪枝后，f1score={f1score}，accuracy={accuracy}，precision={precision}，recall={recall}")

    # 3.2- 保存剪枝后的模型
    torch.save(model.state_dict(),config.after_prune_path)

