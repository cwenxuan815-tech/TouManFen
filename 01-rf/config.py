import os

# 目录锚点（基于本文件位置，与运行时工作目录无关）
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)

# 实验标签：用于「清洗集重训」时把预处理文件/模型另存，避免覆盖旧产物
#   用法：TMF_EXPERIMENT_TAG=_clean  ->  rf_clean.pkl / tfidf_clean.pkl / process_train_data_clean.csv
_TAG = os.environ.get("TMF_EXPERIMENT_TAG", "")


class Config:
    def __init__(self):
        # 1- 原始文件路径（TMF_TRAIN_PATH 可指向清洗后的训练集，例如 00-data/data/train.clean.txt）
        # 训练集
        self.train_datapath = os.environ.get(
            "TMF_TRAIN_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "train.txt"))
        # 测试集
        self.test_datapath = os.environ.get(
            "TMF_TEST_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "test.txt"))
        # 验证集
        self.dev_datapath = os.environ.get(
            "TMF_DEV_PATH", os.path.join(_PROJECT_ROOT, "00-data", "data", "dev.txt"))
        # 类别种类
        self.class_datapath = os.path.join(_PROJECT_ROOT, "00-data", "data", "class.txt")
        # 停用词
        self.stopwords_datapath = os.path.join(_PROJECT_ROOT, "00-data", "data", "stopwords.txt")

        # 2- 分词预处理后文件存放路径
        self.process_train_datapath = os.path.join(_MODULE_DIR, "data", f"process_train_data{_TAG}.csv")
        self.process_test_datapath = os.path.join(_MODULE_DIR, "data", f"process_test_data{_TAG}.csv")
        self.process_dev_datapath = os.path.join(_MODULE_DIR, "data", f"process_dev_data{_TAG}.csv")

        # ------------------------ 理解：简化代码 ------------------------
        self.original_datapath_dict = {
            "train": self.train_datapath,
            "test": self.test_datapath,
            "dev": self.dev_datapath
        }
        self.process_datapath_dict = {
            "train": self.process_train_datapath,
            "test": self.process_test_datapath,
            "dev": self.process_dev_datapath
        }

        # 3- 训练好的模型保存路径
        self.tfidf_model_path = os.path.join(_MODULE_DIR, "save_model", f"tfidf{_TAG}.pkl")
        self.rf_model_path = os.path.join(_MODULE_DIR, "save_model", f"rf{_TAG}.pkl")

        # 4- 预测结果存储路径
        self.predict_result_path = os.path.join(_MODULE_DIR, "result", f"dev{_TAG}.csv")
