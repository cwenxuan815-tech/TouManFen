# Bert预训练模型 + 我们自定义的网络层 -> 分类的目的
import torch
import torch.nn as nn
from config import Config
from transformers import BertModel
from transformers import BertConfig

config = Config()

class BertClassifierModel(nn.Module):
    def __init__(self):
        # 1- 初始化父类
        super().__init__()

        # 2- 搭建网络结构
        # 2.1- 先定义Bert模型
        self.bert_model = BertModel.from_pretrained(config.bert_path)

        # 2.2- 再定义我们自己的网络结构
        in_features = BertConfig.from_pretrained(config.bert_path).hidden_size
        self.linear = nn.Linear(in_features=in_features,out_features=config.classname_len)

    def forward(self,input_ids,attention_mask):
        # torch.no_grad()冻结bert的反向传播。如果放开，训练耗时大量增加
        with torch.no_grad():
            bert_output = self.bert_model(input_ids=input_ids,attention_mask=attention_mask)

        # 调用我们自己的网络层
        """
            last_hidden_state[:,0]和pooler_output，实际是类似的东西，都表示[CLS]的隐藏状态。
            区别：需要对last_hidden_state[:,0]经过nn.Linear和激活函数处理后，才能得到pooler_output
            对应源代码位置：BertModel文件的697行
            【推荐】：使用last_hidden_state[:,0]
        """
        # 下面两行代码的效果类似
        # return self.linear(bert_output.pooler_output)
        return self.linear(bert_output.last_hidden_state[:,0])
