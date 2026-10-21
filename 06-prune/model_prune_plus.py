# 模型剪枝：最核心代码
# 需求：BERT 全局非结构化剪枝：对所有 encoder 层注意力权重剪枝 30%，L1 范数
from data_preprocessing import build_dataloader
from model_eval import eval_model
from config import Config
from bert_model import BertClassifierModel
import torch
from torch.nn.utils import prune    # 模型剪枝方法

# 打印权重稀疏度
def compute_sparsity(model):
    """
    计算权重稀疏度
    :param model:
    :return: 稀疏度
    """
    total_params = 0    # Bert预训练模型中参数中个数
    zero_params = 0     # 剪枝后参数值为0的参数个数
    layer_num = len(model.bert_model.encoder.layer)

    for i in range(layer_num):
        weight = model.bert_model.encoder.layer[i].attention.self.query.weight
        total_params += weight.numel()  # 获得权重张量中的参数个数
        zero_params += (weight == 0).sum().item()
    return (zero_params*100.0 / total_params) if total_params > 0 else 0


def print_weights(weight, name, rows=5, cols=5):
    """
    打印权重前n行，前n列
    :param weight: 权重
    :param name: 名称
    :param rows: 前rows行
    :param cols: 前clos列
    :return:
    """
    print(f"\n{name}（前 {rows}x{cols}）：")
    print(weight[:rows, :cols])

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
    # 1.3- 打印参数信息
    zero_param_rate = compute_sparsity(model)
    print(f"剪枝前 参数值为0的占比{zero_param_rate}")
    # 只对编码器层中第一层的query的权重进行抽样展示
    print_weights(weight=model.bert_model.encoder.layer[0].attention.self.query.weight,name="剪枝前：权重")


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

    # 3.3- 打印参数信息
    zero_param_rate = compute_sparsity(model)
    print(f"剪枝后 参数值为0的占比{zero_param_rate}")
    # 只对编码器层中第一层的query的权重进行抽样展示
    print_weights(weight=model.bert_model.encoder.layer[0].attention.self.query.weight, name="剪枝后：权重")

