import os

# ---------------------------------------------------------------
# 预训练模型路径：默认取「本工程目录的同级 PretrainedModel/bert-base-chinese」
# 可用环境变量 TMF_BERT_PATH 覆盖（例如模型放在别处时）
# ---------------------------------------------------------------
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)
_DEFAULT_BERT = os.path.join(os.path.dirname(_PROJECT_ROOT), "PretrainedModel", "bert-base-chinese")
_BERT_PATH = os.environ.get("TMF_BERT_PATH", _DEFAULT_BERT)

import torch

class Config:
    def __init__(self):
        # 1- 设备
        self.device = ("cuda" if torch.cuda.is_available() else "cpu")

        # 2- 原始文件路径
        self.train_datapath = os.path.join(_MODULE_DIR, "data", "train.txt")
        self.dev_datapath = os.path.join(_MODULE_DIR, "data", "dev.txt")
        self.test_datapath = os.path.join(_MODULE_DIR, "data", "test.txt")
        self.class_datapath = os.path.join(_MODULE_DIR, "data", "class.txt")

        # 3- 数据加载器参数
        self.batch_size = 64    # 64条新闻
        self.max_length = 32    # 句子中词的个数最多是32个词

        # 4- Bert预训练模型的路径
        self.bert_path = _BERT_PATH

        # 5- 目标值文件解析
        self.classname_list = [line.strip() for line in open(self.class_datapath,mode="r",encoding="UTF-8")]
        self.classname_len = len(self.classname_list)

        # 6- 训练好的【教师模型】的保存路径
        self.teacher_save_model = os.environ.get(
            "TMF_TEACHER_SAVE", os.path.join(_MODULE_DIR, "save_model", "teacher_bert.pkl"))

        # 7- 训练好的【学生模型】的保存路径
        #    v1（model_distll.py）默认存 student_bert.pkl；v2（distill_v2.py）实验时可用 TMF_STUDENT_SAVE 另存，避免覆盖
        self.student_save_model = os.environ.get(
            "TMF_STUDENT_SAVE", os.path.join(_MODULE_DIR, "save_model", "student_v2b.pkl"))

if __name__ == '__main__':
    config = Config()
    print(config.classname_list)
    print(config.classname_len)