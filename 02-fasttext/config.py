import os

# 目录锚点（基于本文件位置，与运行时工作目录无关）
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)

# 实验标签：清洗集重训时另存产物，避免覆盖旧模型
#   用法：TMF_EXPERIMENT_TAG=_clean -> word_auto_train_clean.pkl / process_word_train_clean.txt
_TAG = os.environ.get("TMF_EXPERIMENT_TAG", "")

class Config:
    def __init__(self):

        # 1- 原始文件路径（TMF_TRAIN_PATH 可指向清洗后的训练集）
        self.train_datapath = os.environ.get(
            "TMF_TRAIN_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "train.txt"))
        self.test_datapath = os.environ.get(
            "TMF_TEST_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "test.txt"))
        self.dev_datapath = os.environ.get(
            "TMF_DEV_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "dev.txt"))
        self.class_datapath = os.path.join(_PROJECT_ROOT, "00-data", "data", "class.txt")

        # 2- 预处理后的文件路径：字符级
        self.process_char_train_datapath = os.path.join(_MODULE_DIR, "data", f"process_char_train{_TAG}.txt")
        self.process_char_test_datapath = os.path.join(_MODULE_DIR, "data", f"process_char_test{_TAG}.txt")
        self.process_char_dev_datapath = os.path.join(_MODULE_DIR, "data", f"process_char_dev{_TAG}.txt")

        # 3- 预处理后的文件路径：词级
        self.process_word_train_datapath = os.path.join(_MODULE_DIR, "data", f"process_word_train{_TAG}.txt")
        self.process_word_test_datapath = os.path.join(_MODULE_DIR, "data", f"process_word_test{_TAG}.txt")
        self.process_word_dev_datapath = os.path.join(_MODULE_DIR, "data", f"process_word_dev{_TAG}.txt")

        # 4- 将class.txt文件内容转成字典。格式如下：
        # {0: 'finance', 1: 'realty'...}
        self.id2label = {index:class_name.strip() for index,class_name in enumerate(open(self.class_datapath,mode="r",encoding="UTF-8"))}

        # 5- 训练好模型的保存路径
        self.model_char_manual_train = os.path.join(_MODULE_DIR, "save_model", f"char_manual_train{_TAG}.pkl")
        self.model_char_auto_train = os.path.join(_MODULE_DIR, "save_model", f"char_auto_train{_TAG}.pkl")
        self.model_word_manual_train = os.path.join(_MODULE_DIR, "save_model", f"word_manual_train{_TAG}.pkl")
        self.model_word_auto_train = os.path.join(_MODULE_DIR, "save_model", f"word_auto_train{_TAG}.pkl")

if __name__ == '__main__':
    config = Config()
    print(config.id2label)
