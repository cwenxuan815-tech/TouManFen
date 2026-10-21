import pandas as pd
from config import Config
from sklearn.feature_extraction.text import TfidfVectorizer # TF-IDF类
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier # 随机森林
from tqdm import tqdm
from sklearn.metrics import precision_score,recall_score,f1_score
import pickle   # 专门读写pickle类型的文件

# 1- 基础配置信息
config = Config()

if __name__ == '__main__':
    # 2- 读取预处理后的文件
    # 2.1- 读取文件
    # [:2000]：只取前2000行数据，为了加快代码开发和测试的速度。注意：正式上线之前，要删掉
    # df = pd.read_csv(config.process_train_datapath)[:2000]
    df = pd.read_csv(config.process_train_datapath)

    # 2.2- 获取特征和目标值
    words = df["words"]
    y = df["label"]

    # 3- 特征数据转词向量
    # 3.1- 创建TF-IDF算法模型实例对象
    # 读取停止词
    stop_word_list = open(config.stopwords_datapath,encoding="UTF-8").read().split()
    tfidf = TfidfVectorizer(stop_words=stop_word_list)
    # 模型训练，并且对数据转成词向量
    x = tfidf.fit_transform(words)

    # 3.2- 输出相关信息
    # print("处理后的特征名称列表",tfidf.get_feature_names_out())
    # print("处理后的词和索引映射关系",tfidf.vocabulary_)
    # print("处理后的词和索引的条数：也就是词汇表大小",len(tfidf.vocabulary_))

    """
        TF-IDF处理后的数据解释：
            Coords	Values
            (0, 818)	0.4145423492151699
            
            1- 0：代表该词所在的句子索引
            2- 818：代表该词的词索引
            3- 0.4145423492151699：TF-IDF的值
    """
    # print(x)

    # 4- 划分训练集和测试集
    x_train,x_test,y_train,y_test = train_test_split(x,y,test_size=0.2,random_state=114)

    # 5- 模型训练
    model = RandomForestClassifier(n_jobs=-1)
    for _ in tqdm(range(1)):
        model.fit(x_train,y_train)

    # 6- 评估
    y_pred = model.predict(x_test)
    """
        average参数解释：
            1- binary：针对二分类场景
            2- macro：宏观。多分类场景，样本分布均衡的场景
            3- micro：微观。多分类场景，样本分布不均衡的场景
    """
    print("精确率",precision_score(y_test, y_pred, average="macro"))
    print("召回率",recall_score(y_test, y_pred, average="macro"))
    print("F1值",f1_score(y_test, y_pred, average="macro"))


    # 7- 保存训练好的模型：推荐使用joblib.dump()
    with open(config.tfidf_model_path,mode="wb") as f:
        pickle.dump(tfidf,f)

    with open(config.rf_model_path,mode="wb") as f:
        pickle.dump(model,f)



