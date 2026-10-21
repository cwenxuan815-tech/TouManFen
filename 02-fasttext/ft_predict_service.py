from config import Config
import fasttext
import jieba

config = Config()

# 1- 加载训练好的模型：因为词级的模型效果最好
model = fasttext.load_model(config.model_word_auto_train)

# 2- 预测函数
def predict(news_data):
    """
    对用户输入的新闻标题进行分类预测
    :param news_data: 字典。格式：{"title":新闻标题}
    :return: 字典。格式：{"title":新闻标题, "pred_class":分类预测结果名称}
    """
    # 1- 【可选】增加健壮性的代码
    if not news_data.__contains__("title"):
        news_data["error"] = "传递的参数中没有title字段"
        return news_data

    # 2- 取出新闻标题；数据预处理，也就是分词
    title = " ".join(jieba.lcut(news_data["title"]))

    # 3- 预测
    # 返回值类型是嵌套元组。格式：(('__label__science',), array([0.81338769]))
    pred_result = model.predict(title)
    # print(type(pred_result))
    # print(pred_result)

    # 4- 取出预测结果
    result = pred_result[0][0].replace("__label__","")

    # 5- 返回结果
    news_data["pred_class"] = result
    return news_data

if __name__ == '__main__':
    # news_data = {"title":"体验2D巅峰 倚天屠龙记十大创新概览"}
    news_data = {"aaaa":"体验2D巅峰 倚天屠龙记十大创新概览"}
    result = predict(news_data)
    print(result)






