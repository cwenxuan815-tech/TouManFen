import os

# 目录锚点（基于本文件位置，与运行时工作目录无关）
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)

# 把常用的变量值放在类中。目的是为了方便使用

class Config:
    def __init__(self):
        # 1- 训练集文件路径
        self.train_datapath = os.path.join(_PROJECT_ROOT, "data", "train.txt")
        # 2- 测试集文件路径
        self.test_datapath = os.path.join(_PROJECT_ROOT, "data", "test.txt")
        # 3- 验证集文件路径
        self.dev_datapath = os.path.join(_PROJECT_ROOT, "data", "dev.txt")

if __name__ == '__main__':
    config = Config()
    print(config.train_datapath)