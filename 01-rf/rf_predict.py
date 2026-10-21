import pandas as pd
import pickle
from config import Config
from sklearn.feature_extraction.text import TfidfVectorizer # TF-IDF类
from sklearn.ensemble import RandomForestClassifier # 随机森林
from sklearn.metrics import precision_score,recall_score,f1_score

config = Config()

if __name__ == '__main__':
    # 1- 读取验证集数据
    df = pd.read_csv(config.process_dev_datapath,encoding="UTF-8")
    # 获得特征和目标值
    words = df["words"]
    y = df["label"]

    # 2- 加载训练好的模型
    with open(config.rf_model_path,mode="rb") as f:
        model:RandomForestClassifier = pickle.load(f)

    with open(config.tfidf_model_path,mode="rb") as f:
        tfidf:TfidfVectorizer = pickle.load(f)

    # 3- 对数据集中words进行TF-IDF的处理
    x = tfidf.transform(words)

    # 4- 预测
    y_pred = model.predict(x)

    # 5- 输出指标
    print("预测的精确率：",precision_score(y,y_pred,average="macro"))
    print("预测的召回率：",recall_score(y,y_pred,average="macro"))
    print("预测的F1值：",f1_score(y,y_pred,average="macro"))

    # 6- 将预测结果作为新字段放到DF中，然后写出成文件
    df["pred"] = y_pred
    df.to_csv(config.predict_result_path,sep="\t",index=False,header=True)