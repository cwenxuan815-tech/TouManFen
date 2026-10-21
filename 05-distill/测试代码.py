import torch

if __name__ == '__main__':
    cls_token_index = 3  # 句子开头
    sep_token_index = 4  # 句子结尾

    input_ids = torch.randint(low=1,high=6,size=(2,5))
    attentition_mask = torch.randint(low=0,high=2,size=(2,5))

    print(f"输入的句子词索引input_ids：\n{input_ids}")
    print(f"输入的句子掩码attentition_mask：\n{attentition_mask}")

    ebd_mask = (input_ids != cls_token_index) & (input_ids != sep_token_index)
    print(f"输入的句子处理后的掩码ebd_mask_1：\n{ebd_mask}")

    ebd_mask = ebd_mask & attentition_mask
    print(f"输入的句子处理后的掩码ebd_mask_2：\n{ebd_mask}")

    print("-"*30)

    ebd_mask = ebd_mask.unsqueeze(-1)
    ebd = torch.randint(low=1,high=10,size=(2,5,4))
    print(f"词向量的掩码ebd_mask：{ebd_mask}")
    print(f"前_词向量ebd：{ebd}")

    ebd = ebd * ebd_mask
    print(f"后_词向量ebd：{ebd}")

    print("1   ", ebd.sum(dim=1))
    print("1   ", ebd.sum(dim=1).shape)
    print("-1  ", ebd.sum(dim=-1).shape)