# 注意：DQ推荐在CPU环境下运行
from config import Config
from bert_model import BertClassifierModel
import torch
from data_preprocessing import build_dataloader
from bert_train_and_eval import eval_model

if __name__ == '__main__':
    # 1- 【了解】查看支持的量化引擎
    """
        量化引擎：
            none：没有硬件加速
            onednn：英特尔深度学习
            x86：x86架构的服务器进行优化
            fbgemm：FaceBook的量化计算引擎
    """
    print("支持的量化引擎：",torch.backends.quantized.supported_engines)

    # 2- 【了解】显示设置量化引擎：onednn
    # 上面两行代码了解的原因是：代码会自动根据你的环境自动的决定用什么量化引擎
    torch.backends.quantized.engine = "onednn"

    # 3- 基础配置
    config = Config()

    # 4- 量化前
    # 加载模型
    model = BertClassifierModel()
    # map_location：加载到CPU上
    model.load_state_dict(torch.load(config.save_model,map_location="cpu"))
    model.eval()
    f1score, accuracy, precision, recall = eval_model(model)
    print(f"量化前的指标{f1score}，{accuracy}，{precision}，{recall}")

    # 5- 量化后
    # 【DQ量化核心代码】
    """
        参数解释：
            model：要量化的模型对象
            qconfig_spec：要对模型中的什么网络层进行量化
            dtype：将网络层中的参数缩小到什么数据类型
    """
    quantization_model = torch.quantization.quantize_dynamic(model,qconfig_spec={torch.nn.Linear},dtype=torch.qint8)
    quantization_model.eval()
    f1score2, accuracy2, precision2, recall2 = eval_model(quantization_model)
    print(f"量化后的指标{f1score2}，{accuracy2}，{precision2}，{recall2}")

    # 保存量化后的模型
    torch.save(quantization_model,"save_model/quantization_model.pkl")



