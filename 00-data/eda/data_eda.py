# 数据探索：目的是为了让自己对数据的内容、含义、分布理解的更加清楚

import pandas as pd
from config import Config
from collections import Counter
import matplotlib.pyplot as plt

# 1- 获得配置信息
config = Config()
datapath = config.train_datapath
datapath = config.test_datapath

if __name__ == '__main__':
    # 2- 读取文件内容
    """
        参数解释：
            sep：字段值之间的分隔符
            names：自定义字段名称。如果原始文件中也有字段名称，会用names覆盖掉文件中的
    """
    df = pd.read_csv(datapath,sep="\t",encoding="UTF-8",names=["text","label"])
    # print(df.head())


    # 3- 统计目标值的分布情况
    # 3.1- 统计每种目标值的样本条数
    label_counts = Counter(df["label"])
    # print(label_counts)

    # 3.2- 统计每种目标值的样本条数占比
    total_count = df["label"].size  # 总样本条数
    for label,cnt in label_counts.items():
        rate = cnt*100/total_count
        print(f"目标值={label}，条数：{cnt}，占比：{rate}%")

    # 4- 句子长度分布
    # 4.1- 统计每条句子的长度
    df["length"] = df["text"].str.len()
    print(df.head())

    # 4.2- 最长、最短、均值、中位数等的统计
    print("句子最大长度：",df["length"].max())
    print("句子最短长度：",df["length"].min())
    print("句子均值长度：",df["length"].mean())
    print("句子中位数长度：",df["length"].median())

    # 4.3- 绘制直方图
    df["length"].hist()
    plt.show()