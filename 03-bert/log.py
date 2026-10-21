# -*- coding: utf-8 -*-
import logging
import os

"""
    0- 为什么需要用日志记录，而不使用print？
        print的内容会输出到控制台，不会记录到文件中。日志的方式会记录到文件，是一个持久化存储
    
    1- 为什么不从头开发日志记录代码？
        实际工作中，大多数情况是从已有的旧项目中复制过来，直接使用
        或者如果公司的AI小组刚成立，直接通过大模型生成，拿着用即可
        
    2- 实际工作中日志级别如何设置？
        2.1- 日志级别由低到高排序：debug、info、warning、error、crit
        2.2- 实际工作中如何设置：
            a、本地开发、测试人员测试你的代码，一般设置为debug，或者设置为info
            b、正式环境运行（项目上线），一般设置为warning、error、info。
                注意：一定不可以设置为debug或者crit
                debug会导致日志非常多
                crit会导致error、warning得不到记录
"""
class Logger(object):
    # 日志级别关系映射
    level_relations = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        'crit': logging.CRITICAL
    }

    def __init__(self, root_path, log_name, level='info', fmt='%(asctime)s - %(levelname)s: %(message)s'):
        # 指定日志保存的路径
        self.root_path = root_path

        # 初始logger名称和格式
        self.log_name = log_name

        # 初始格式
        self.fmt = fmt

        # 先声明一个 Logger 对象
        self.logger = logging.getLogger(log_name)

        # 设置日志级别
        self.logger.setLevel(self.level_relations.get(level))

    def get_logger(self):
        # 指定对应的 Handler 为 FileHandler 对象， 这个可适用于多线程情况
        path = os.path.join(self.root_path, 'log')
        os.makedirs(path, exist_ok=True)
        file_name = os.path.join(path, self.log_name + '.log')
        rotate_handler = logging.FileHandler(file_name, encoding="utf-8", mode="a")

        # Handler 对象 rotate_handler 的输出格式
        formatter = logging.Formatter(self.fmt)
        rotate_handler.setFormatter(formatter)

        # 将rotate_handler添加到Logger
        self.logger.addHandler(rotate_handler)

        return self.logger

if __name__ == '__main__':
    # log_obj = Logger(root_path="../../",log_name="日志文件测试",level="info")
    log_obj = Logger(root_path="../../",log_name="日志文件测试",level="error")
    logger = log_obj.get_logger()

    logger.debug("这是一条debug日志")
    logger.info("这是一条info日志")
    logger.error("这是一条error日志")

