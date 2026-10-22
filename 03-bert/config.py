import os
import sys

# ---------------------------------------------------------------
# 目录锚点（基于本文件位置，与运行时工作目录无关）
# 预训练模型路径：默认取「本工程目录的同级 PretrainedModel/bert-base-chinese」
# 可用环境变量 TMF_BERT_PATH 覆盖（例如模型放在别处时）
# 目录结构应为：config.json / vocab.txt / tokenizer.json / pytorch_model.bin
# ---------------------------------------------------------------
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)
_DEFAULT_BERT = os.path.join(os.path.dirname(_PROJECT_ROOT), "PretrainedModel", "bert-base-chinese")
_BERT_PATH = os.environ.get("TMF_BERT_PATH", _DEFAULT_BERT)

# tmf_common 位于工程根：本文件可能被训练脚本（直接运行）或 worker（薄壳）导入，
# 这里显式保证工程根可导入，不依赖调用方的 sys.path。
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tmf_common.config import (  # noqa: E402
    DEFAULT_TORCH_THREADS,
    TORCH_THREADS_MAX,
    TORCH_THREADS_MIN,
    env_int,
)


class Config:
    def __init__(self):
        # 1- 设备
        # 默认 cpu：动态量化(DQ)必须在 CPU 上执行。
        # 若要用 GPU 做全量微调，设置环境变量 TMF_DEVICE=cuda
        self.device = os.environ.get("TMF_DEVICE", "cpu")

        # 1.0- 推理线程数（B12）：默认 4，可用 TMF_TORCH_THREADS 覆盖（1~64）。
        #      由 bert_predict_service 在模型加载前调用 torch.set_num_threads。
        self.torch_threads = env_int(
            "TMF_TORCH_THREADS",
            DEFAULT_TORCH_THREADS,
            TORCH_THREADS_MIN,
            TORCH_THREADS_MAX,
        )

        # 2- 原始文件路径（可用 TMF_TRAIN_PATH / TMF_DEV_PATH 覆盖，便于小样本冒烟测试）
        self.train_datapath = os.environ.get("TMF_TRAIN_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "train.txt"))
        self.dev_datapath = os.environ.get("TMF_DEV_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "dev.txt"))
        self.test_datapath = os.environ.get("TMF_TEST_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "test.txt"))
        self.class_datapath = os.path.join(_PROJECT_ROOT, "00-data", "data", "class.txt")

        # 3- 数据加载器参数
        self.batch_size = int(os.environ.get("TMF_BATCH_SIZE", "64"))    # 64条新闻
        self.max_length = int(os.environ.get("TMF_MAX_LENGTH", "32"))    # 句子中词的个数最多是32个词

        # 4- Bert预训练模型的路径
        self.bert_path = _BERT_PATH

        # 5- 目标值文件解析
        self.classname_list = [line.strip() for line in open(self.class_datapath, mode="r", encoding="UTF-8")]
        self.classname_len = len(self.classname_list)

        # 6- 日志文件存储目录
        self.log_path = _MODULE_DIR + os.sep

        # 7- 训练好模型的保存路径
        self.save_model = os.environ.get("TMF_SAVE_MODEL", os.path.join(_MODULE_DIR, "save_model", "bert.pkl"))

        # ================= 训练超参数（均可用环境变量覆盖） =================
        # 8- 是否冻结 BERT 编码器
        #    TMF_FREEZE_BERT=1 -> 参考代码原样（冻结编码器，只训线性头；CPU 约 5.3h，天花板约 0.87）
        #    TMF_FREEZE_BERT=0 -> B 方案：解冻全量微调（CPU 约 8h/epoch，可达 0.93+）
        self.freeze_bert = os.environ.get("TMF_FREEZE_BERT", "0") == "1"

        # 9- 训练轮数 / 学习率 / 验证与日志间隔
        self.num_epochs = int(os.environ.get("TMF_EPOCHS", "1"))
        self.learning_rate = float(os.environ.get("TMF_LR", "5e-5"))
        self.eval_interval = int(os.environ.get("TMF_EVAL_INTERVAL", "500"))   # 每多少个 batch 在 dev 上评估一次
        self.log_interval = int(os.environ.get("TMF_LOG_INTERVAL", "50"))      # 每多少个 batch 打印一次进度

        # 10- 训练日志文件（长时间运行时可用 tail 观察）
        self.train_log = os.environ.get("TMF_TRAIN_LOG", os.path.join(_MODULE_DIR, "log", "train_ft.log"))


if __name__ == '__main__':
    config = Config()
    print("device        :", config.device)
    print("torch_threads :", config.torch_threads, "（TMF_TORCH_THREADS，默认 4）")
    print("bert_path     :", config.bert_path, "存在:", os.path.isdir(config.bert_path))
    print("freeze_bert   :", config.freeze_bert, "（False = B 方案 全量微调）")
    print("num_epochs    :", config.num_epochs, "| lr:", config.learning_rate)
    print("eval_interval :", config.eval_interval, "| batch_size:", config.batch_size)
    print("train/dev     :", config.train_datapath, "|", config.dev_datapath)
    print("save_model    :", config.save_model)
    print("classname_list:", config.classname_list)
