import torch
from torch import Tensor

from config import Config
import torch.nn as nn
from transformers import BertConfig

config = Config()
bert_config = BertConfig.from_pretrained(config.bert_path)

class BiLSTMStudentModel(nn.Module):
    def __init__(self):
        # 1- 初始化父类
        super().__init__()

        # 2- 设置属性值
        self.vocab_size = bert_config.vocab_size
        self.embedding_dim = 128
        self.hidden_size = 256
        self.num_layers = 3

        # 3- 搭建网络结构
        # 3.1- 词嵌入层
        # embedding_dim：由我们自己设置，与教师模型没有任何关系
        self.embedding = nn.Embedding(num_embeddings=self.vocab_size,embedding_dim=self.embedding_dim)

        # 3.2- 循环神经网络层：BiLSTM
        """
            参数解释：
                input_size：输入的词向量维度。必须和上面的embedding_dim相同
                hidden_size：隐藏层向量维度。大小自定义
                num_layers：隐藏层的层数。大小自定义，与是否是双向无关
                batch_first：输入和输出的数据中，batch_size是否在张量的第一个位置上
                bidirectional：是否是双向LSTM
        """
        self.lstm = nn.LSTM(
            input_size=self.embedding_dim,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            batch_first=True,
            bidirectional=True
        )

        # 3.3- 随机失活层
        self.dropout = nn.Dropout(p=0.3)

        # 3.4- 线性层
        """
            为什么是self.hidden_size*2？
            答：因为是双向的LSTM。双向的LSTM执行完以后，会将两个方向的隐藏状态信息进行concat拼接，形状变成两倍了
        """
        self.linear = nn.Linear(in_features=self.hidden_size*2,out_features=config.classname_len)

    def forward(self,input_ids, attentition_mask):
        """
        :param input_ids: 句子中词索引
        :param attentition_mask: 输入句子的掩码
        :return:
        """

        # 1- 调用词嵌入层，得到词向量
        # 张量形状：[batch_size, seq_len, embedding_dim]
        ebd = self.embedding(input_ids)

        # 2- 对词向量进行掩码操作：最终目的是只对正常词进行处理，句子开头、句子结尾、padding填充的0这三者不关心
        cls_token_index = 101   # 句子开头
        sep_token_index = 102   # 句子结尾
        ebd_mask = (input_ids!=cls_token_index) & (input_ids!=sep_token_index)
        ebd_mask:Tensor = ebd_mask & attentition_mask

        # 对词向量进行掩码操作
        # [batch_size, seq_len] 升维至 [batch_size, seq_len, 1]
        ebd_mask = ebd_mask.unsqueeze(-1)
        ebd = ebd * ebd_mask

        # 3- 调用循环神经网络BiLSTM
        # 为什么调用lstm的时候，没有手动传递初始的细胞状态和隐藏状态：LSTM内部会自动的进行全0初始化。源代码在1056行
        output,(hidden,c) = self.lstm(ebd)

        # 4- 计算平均池化值
        """
            下面两个地方都是sum(dim=1)的原因：
                1- output的张量形状[batch_size,seq_len,hidden_size]
                2- 我们的业务需求是对句子进行分类，因此主体是句子
                3- sum(dim=1)得到的结果形状[batch_size,hidden_size]。形状的含义是把一条句子中所有词的词向量加起来，
                    得到句子级别的向量总和
                    
        """
        # 分子：所有有效的词的向量之和
        output_sum = output.sum(dim=1)
        # 分母：所有有效的词总数。1e-6为了防止分母为0
        token_count = ebd_mask.sum(dim=1) + 1e-6
        new_output = output_sum/token_count

        # 5- 调用线性层，得到预测结果，并返回
        return self.linear(self.dropout(new_output))

if __name__ == '__main__':
    # 编写测试数据
    input_ids = torch.LongTensor([[101, 2, 3, 4, 5], [4, 102, 6, 7, 8]])
    attention_mask = torch.LongTensor([[1, 1, 1, 1, 0], [1, 1, 0, 0, 0]])
    model = BiLSTMStudentModel()
    print(model(input_ids, attention_mask))
