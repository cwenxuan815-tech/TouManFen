"""
    数据预处理要求：
        原始数据：今天天气真的很好,8
        处理后数据：__label__game 今天 天气 真的 很好
"""
import jieba
from config import Config
config = Config()


def preprocessing(datapath, process_datapath, is_char=True):
    """
    FastText中有监督学习的数据预处理
    :param datapath: 原始文件路径
    :param process_datapath: 预处理后的文件路径
    :param is_char: 是否是字级别的处理，默认True，也就是字级别。False使用jieba分词，词级别
    :return: None
    """

    # 1- 读取原始文件内容
    with open(datapath,mode="r",encoding="UTF-8") as f:
        lines = f.readlines()

    # 2- 预处理
    with open(process_datapath, mode="w", encoding="UTF-8") as f:
        # 2.1- 遍历原始文件内容
        for line in lines:
            # 去除空行
            # 注意：需要先执行strip()，避免空行中有空格的情况
            line = line.strip()
            if line=="":
                continue

            # 2.2- 拆分得到新闻标题和目标值
            title,label = line.split("\t")

            # 2.3- 对新闻标题进行处理
            if is_char:
                title = " ".join(list(title))
            else:
                title = " ".join(jieba.lcut(title))

            # 2.4- 对目标值进行处理
            # 字符串类型转成数字
            label = int(label)
            # 通过key获取对应的类别名称
            label_name = config.id2label[label]

            # 2.5- 目标值和新闻标题拼接成如下的格式
            # __label__目标值 处理后的新闻标题
            new_line = f"__label__{label_name} {title}\n"

            # 2.6- 写入到新文件中
            f.write(new_line)

if __name__ == '__main__':
    # 字符级
    preprocessing(datapath=config.train_datapath, process_datapath=config.process_char_train_datapath, is_char=True)
    preprocessing(datapath=config.dev_datapath, process_datapath=config.process_char_dev_datapath, is_char=True)
    preprocessing(datapath=config.test_datapath, process_datapath=config.process_char_test_datapath, is_char=True)

    # 词级
    preprocessing(datapath=config.train_datapath, process_datapath=config.process_word_train_datapath, is_char=False)
    preprocessing(datapath=config.dev_datapath, process_datapath=config.process_word_dev_datapath, is_char=False)
    preprocessing(datapath=config.test_datapath, process_datapath=config.process_word_test_datapath, is_char=False)

