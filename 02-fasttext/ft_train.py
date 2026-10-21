import fasttext
from config import Config

config = Config()

"""
    使用Fasttext实现如下四种模型训练：
        1- 字符级
            1.1- 手动设置超参数 char_manual_train
            1.2- 自动调整超参数 char_auto_train
            
        2- 词级
            2.1- 手动设置超参数 word_manual_train
            2.2- 自动调整超参数 word_auto_train
"""

def char_manual_train():
    # 1- 模型训练：有监督学习
    """
        参数解释：
            dim：词向量维度
            epoch：训练轮次
            minn、maxn：是n-gram中n的取值范围，左右都是闭区间。不管你输入进来的是啥，先在内容的前后增加<>，然后再分词
    """
    model = fasttext.train_supervised(
        input=config.process_char_train_datapath,
        dim=100,
        epoch=50,
        minn=1,
        maxn=4
    )

    # 2- 保存训练好的模型
    model.save_model(config.model_char_manual_train)

    # 3- 模型评估
    # test返回值解释：样本条数、精确率、召回率
    result = model.test(config.process_char_test_datapath)
    print(f"字符级_手动设置超参数_评估结果：{result}")

    # 4- 其他操作
    # 4.1- 使用训练好的模型进行预测
    pred_result = model.predict("房 山 纯 新 盘 绿 地 新 都 会 国 际 花 都 1 1 月 开 盘")
    print(f"预测结果：{pred_result}")

    # 4.2- 查看模型词表信息
    words = model.words
    print(type(words))  # List列表
    print(len(words))
    print(words[:10])

    # 4.3- 子词：查看minn和maxn的作用
    print("子词",model.get_subwords("ab"))

    # 4.4- 词的维度
    print("词的维度",model.get_dimension())

def char_auto_train():
    # 1- 模型训练
    """
        参数解释：
            verbose：用来控制自动调参过程中日志的展示级别。该值越大，信息越丰富
    """
    model = fasttext.train_supervised(
        input=config.process_char_train_datapath,
        autotuneValidationFile=config.process_char_dev_datapath,
        autotuneDuration=3*60,
        seed=115,
        verbose=3
    )

    # 2- 保存训练好的模型
    model.save_model(config.model_char_auto_train)

    # 3- 评估
    result = model.test(config.process_char_test_datapath)
    print(f"字符级_自动设置超参数_评估结果：{result}")

def word_manual_train():
    # 1- 模型训练：有监督学习
    model = fasttext.train_supervised(
        input=config.process_word_train_datapath,
        dim=100,
        epoch=50,
        minn=1,
        maxn=4
    )

    # 2- 保存训练好的模型
    model.save_model(config.model_word_manual_train)

    # 3- 模型评估
    # test返回值解释：样本条数、精确率、召回率
    result = model.test(config.process_word_test_datapath)
    print(f"词级_手动设置超参数_评估结果：{result}")

    # 4- 子词：查看minn和maxn的作用
    print("子词", model.get_subwords("人工智能"))

def word_auto_train():
    # 1- 模型训练
    model = fasttext.train_supervised(
        input=config.process_word_train_datapath,
        autotuneValidationFile=config.process_word_dev_datapath,
        autotuneDuration=3 * 60,
        seed=115,
        verbose=3
    )

    # 2- 保存训练好的模型
    model.save_model(config.model_word_auto_train)

    # 3- 评估
    result = model.test(config.process_word_test_datapath)
    print(f"词级_自动设置超参数_评估结果：{result}")

if __name__ == '__main__':
    # 1- 字符级
    # 手动设置超参数
    # 字符级_手动设置超参数_评估结果：(10000, 0.8714, 0.8714)
    char_manual_train()

    # 自动调整超参数
    # 字符级_自动设置超参数_评估结果：(10000, 0.873, 0.873)
    char_auto_train()

    # 2- 词级
    # 手动设置超参数
    # 词级_手动设置超参数_评估结果：(10000, 0.9103, 0.9103)
    word_manual_train()

    # 自动调整超参数
    # 词级_自动设置超参数_评估结果：(10000, 0.9143, 0.9143)
    word_auto_train()