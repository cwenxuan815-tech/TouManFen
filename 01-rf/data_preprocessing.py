# 数据预处理：将一条句子分词得到一个个的词

import pandas
import jieba
import pandas as pd

from config import Config

# 1- 加载配置文件
config = Config()

def preprocessing(datapath):
    # 1- 读取对应的文件
    df = pd.read_csv(datapath,sep="\t",encoding="UTF-8",names=["text","label"])

    # 2- 对text句子进行分词，并且只取前30个词
    """
        代码解释：
            jieba.lcut(line)：对句子分词得到List列表，里面放的是一个个词
            jieba.lcut(line)[:30]：对上面的List列表进行切片，只保留前30个词
            " ".join(jieba.lcut(line)[:30])：将List列表中的元素以空格隔开存储到DF中
    """
    df["words"] = df["text"].apply(lambda line: " ".join(jieba.lcut(line)[:30]))
    # print(df.head())

    # 3- 将处理后的DF写入文件进行存储
    """
        to_csv参数解释：
            header：是否将字段名称作为第一行写入到文件中。推荐设置为True
            index：是否将DF中的索引写入到文件中。如果index有业务层面的含义，推荐设置为True；否则就是False
            mode：文件的写入模式。这里是为了防止代码重复跑，然后数据出现重复的问题，因此设置为覆盖写w
    """
    # 3.1- 训练集
    if "train" in datapath:
        df.to_csv(config.process_train_datapath,sep=",",header=True,index=False,mode="w",encoding="UTF-8")
        print("train数据预处理完成，保存成功")

    # 3.2- 测试集
    if "test" in datapath:
        df.to_csv(config.process_test_datapath, sep=",", header=True, index=False, mode="w", encoding="UTF-8")
        print("test数据预处理完成，保存成功")

    # 3.1- 训练集
    if "dev" in datapath:
        df.to_csv(config.process_dev_datapath, sep=",", header=True, index=False, mode="w", encoding="UTF-8")
        print("dev数据预处理完成，保存成功")

def preprocessing_plus(task_type,datapath):
    # 1- 读取对应的文件
    df = pd.read_csv(datapath,sep="\t",encoding="UTF-8",names=["text","label"])

    # 2- 对text句子进行分词，并且只取前30个词
    df["words"] = df["text"].apply(lambda line: " ".join(jieba.lcut(line)[:30]))

    # 3- 将处理后的DF写入文件进行存储
    if task_type in datapath:
        df.to_csv(config.process_datapath_dict.get(task_type),sep=",",header=True,index=False,mode="w",encoding="UTF-8")
        print(f"{task_type}数据预处理完成，保存成功")

if __name__ == '__main__':
    # 2- 获得训练集、测试集、验证集的数据路径；调用数据预处理函数
    # ------------------ 普通版写法 ------------------
    # 2.1- 训练集的处理
    # datapath = config.train_datapath
    # preprocessing(datapath)
    #
    # # 2.2- 测试集的处理
    # datapath = config.test_datapath
    # preprocessing(datapath)
    #
    # # 2.3- 验证集的处理
    # datapath = config.dev_datapath
    # preprocessing(datapath)

    # ------------------ 优化版写法 ------------------
    for task_type,datapath in config.original_datapath_dict.items():
        preprocessing_plus(task_type,datapath)
