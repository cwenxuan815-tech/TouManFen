import jieba
import pickle
from config import Config
from sklearn.feature_extraction.text import TfidfVectorizer # TF-IDF类
from sklearn.ensemble import RandomForestClassifier # 随机森林

# 1- 加载配置文件
config = Config()

# 2- 加载训练好的模型
with open(config.rf_model_path,mode="rb") as f:
    model:RandomForestClassifier = pickle.load(f)

with open(config.tfidf_model_path,mode="rb") as f:
    tfidf:TfidfVectorizer = pickle.load(f)

# 3- 对单条新闻标题进行预测
def predict(news_data):
    """
    对单条新闻标题进行预测
    :param news_data: 字典数据类型。结构：{"title":新闻标题内容}
    :return: 字典。结构：{"title":新闻标题内容,"pred_class":预测的新闻类型}
    """

    # 1- 取出新闻标题；分词；取前30个词；以空格分隔
    # 入参兜底：缺 title 时返回结构化错误（而不是抛 KeyError 变成 500）
    if not isinstance(news_data, dict) or "title" not in news_data:
        news_data = dict(news_data) if isinstance(news_data, dict) else {}
        news_data["error"] = "传递的参数中没有title字段"
        return news_data
    words = " ".join(jieba.lcut(news_data["title"])[:30])

    # 2- TF-IDF进行处理
    x = tfidf.transform([words])

    # 3- 预测
    y_pred_index = model.predict(x)[0]
    # print(y_pred_index)

    # 4- 读取class.txt文件。处理成字典。字典格式 {0:'finance', 1:'realty'....}
    id_class_dict = {i:class_name.strip() for i,class_name in enumerate(open(config.class_datapath,mode="r",encoding="UTF-8"))}
    # print(id_class_dict)

    # 5- 通过预测结果ID获得分类名称
    pred_class_name = id_class_dict[y_pred_index]

    news_data["pred_class"] = pred_class_name

    return news_data

if __name__ == '__main__':
    news_data = {"title":"同步A股首秀：港股缩量回调"}
    result = predict(news_data)

    print(result)







