# 蒸馏学生 v2：BiLSTM + 可由外部注入的预训练词嵌入初始化
#
# 与 v1（student_bilstm_model.py）的区别：
#   1. embedding 维度可配置（默认 256，v1 是 128 硬编码）；
#   2. 支持传入 init_embedding（用教师 BERT 的词嵌入经 SVD 降维得到），解决"随机初始化词向量"的短板；
#   3. LSTM 层数 >1 时启用层间 dropout。
import torch
import torch.nn as nn
from torch import Tensor


class BiLSTMStudentV2(nn.Module):
    def __init__(self, vocab_size, class_len,
                 embedding_dim=256, hidden_size=256, num_layers=3,
                 dropout=0.3, init_embedding=None):
        super().__init__()
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        # 3.1- 词嵌入层（可注入预训练初始化）
        self.embedding = nn.Embedding(num_embeddings=vocab_size, embedding_dim=embedding_dim)
        if init_embedding is not None:
            assert init_embedding.shape == (vocab_size, embedding_dim), \
                f"初始化矩阵形状不符: {tuple(init_embedding.shape)} != {(vocab_size, embedding_dim)}"
            with torch.no_grad():
                self.embedding.weight.copy_(init_embedding)

        # 3.2- 双向 LSTM
        self.lstm = nn.LSTM(
            input_size=embedding_dim,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        # 3.3- 输出层
        self.dropout = nn.Dropout(p=dropout)
        self.linear = nn.Linear(in_features=hidden_size * 2, out_features=class_len)

    def forward(self, input_ids, attention_mask):
        # 1- 词向量 [B, L, D]
        ebd = self.embedding(input_ids)

        # 2- 掩码：去掉 [CLS]=101、[SEP]=102 与 padding
        cls_token_index, sep_token_index = 101, 102
        ebd_mask = (input_ids != cls_token_index) & (input_ids != sep_token_index)
        ebd_mask: Tensor = ebd_mask & attention_mask.bool()
        mask3 = ebd_mask.unsqueeze(-1)
        ebd = ebd * mask3

        # 3- BiLSTM
        output, _ = self.lstm(ebd)

        # 4- 有效词平均池化
        output_sum = (output * mask3).sum(dim=1)
        token_count = mask3.sum(dim=1) + 1e-6
        pooled = output_sum / token_count

        # 5- 分类
        return self.linear(self.dropout(pooled))


if __name__ == '__main__':
    m = BiLSTMStudentV2(vocab_size=21128, class_len=10)
    print("参数量: %.2fM" % (sum(p.numel() for p in m.parameters()) / 1e6))
    ids = torch.LongTensor([[101, 2, 3, 4, 5], [4, 102, 6, 7, 8]])
    mask = torch.LongTensor([[1, 1, 1, 1, 0], [1, 1, 0, 0, 0]])
    print(m(ids, mask))
