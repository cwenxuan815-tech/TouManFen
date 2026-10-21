from config import Config
from data_preprocessing import build_dataloader
from teacher_bert_model import BertTeacherModel # 教师模型
from student_bilstm_model import BiLSTMStudentModel # 学生模型
import torch
import torch.nn as nn
from tqdm import tqdm
from sklearn.metrics import accuracy_score,precision_score,recall_score,f1_score

config = Config()

def eval_model(model):
    # 1- 加载验证集数据
    dev_dataloader = build_dataloader(datapath=config.dev_datapath,shuffle=False)

    # 2- 模型评估
    # 2.1- 定义用来计算准确率的变量
    all_pred_result = []    # 预测结果列表
    all_true_result = []    # 真实结果列表

    model.eval()
    with torch.no_grad():
        for i,batch in enumerate(tqdm(dev_dataloader), start=1):
            # 2.2- 将数据发送到对应设备
            input_ids, attention_mask, labels = batch
            input_ids = input_ids.to(config.device)
            attention_mask = attention_mask.to(config.device)
            labels = labels.to(config.device)

            # 2.3- 前向传播：预测
            pred_output = model(input_ids,attention_mask)
            pred_index = torch.argmax(pred_output,dim=-1)
            # cpu()：因为不涉及张量的计算，因此为了节约GPU资源，可以将数据转到CPU上再处理
            all_pred_result.extend(pred_index.cpu().tolist())
            all_true_result.extend(labels.cpu().tolist())

    # 3- 计算评估指标
    f1score = f1_score(all_true_result,all_pred_result,average="macro")
    # 准确率
    accuracy = accuracy_score(all_true_result,all_pred_result)
    precision = precision_score(all_true_result,all_pred_result,average="macro")
    recall = recall_score(all_true_result,all_pred_result,average="macro")

    return f1score, accuracy, precision, recall

def train_and_eval():
    # 1- 加载数据
    train_dataloader = build_dataloader(datapath=config.train_datapath,shuffle=True)

    # 2- 创建模型
    # 2.1- 加载训练好的教师模型
    teacher_model = BertTeacherModel().to(config.device)
    teacher_model.load_state_dict(torch.load(config.teacher_save_model))
    # 2.2- 新建学生模型
    student_model = BiLSTMStudentModel().to(config.device)

    # 3- 损失函数对象
    loss = nn.CrossEntropyLoss()

    # 4- 优化器对象
    optimizer = torch.optim.AdamW(params=student_model.parameters(),lr=5e-5)

    # 5- 其他变量
    epochs = 1
    best_f1score = 0.0  # f1历史最高分
    T = 2   # 超参数：温度
    alpha = 0.7 # 超参数：软标签权重

    # 6- 训练
    # 6.1- 模型模式设置
    teacher_model.eval()    # 注意：教师模型是已经训练好了，不允许更新参数
    student_model.train()   # 注意：学生模型还未训练

    for epoch in range(epochs):
        for i,batch in enumerate(tqdm(train_dataloader),start=1):
            # 6.2- 将训练数据发送到设备
            input_ids, attention_mask, labels = batch
            input_ids = input_ids.to(config.device)
            attention_mask = attention_mask.to(config.device)
            labels = labels.to(config.device)

            # 6.3- 教师模型_前向传播
            with torch.no_grad():
                teacher_pred = teacher_model(input_ids,attention_mask)
                teacher_pred_labels = torch.argmax(teacher_pred,dim=-1)

            # 6.4- 学生模型_前向传播
            student_pred = student_model(input_ids, attention_mask)
            student_pred_labels = torch.argmax(student_pred, dim=-1)

            # 6.5- 计算KL散度值
            p = torch.log_softmax(teacher_pred/T, dim=-1)
            q = torch.log_softmax(student_pred/T, dim=-1)
            # KL散度值，也就是软标签的值
            """
                注意：kl_div的包不要导错了！！！
                参数解释：
                    input：是【学生模型】输出的结果
                    target：预测结果参考值。也就是【教师模型】输出的结果
                    reduction：上面两个值的计算方式。
                    log_target：是否对计算结果求log对数
            """
            kl_value = torch.nn.functional.kl_div(
                input=q,
                target=p,
                reduction="batchmean",
                log_target=True
            )

            # 6.6- 硬标签损失值
            # 注意：是学生模型的预测概率，与样本的目标值算损失
            hard_label_loss = loss(student_pred,labels)

            # 6.7- 蒸馏的总损失值
            # l = (1-α) * 硬标签损失值 + α * T² * KL散度值
            distll_loss = (1 - alpha) * hard_label_loss + alpha * (T**2) * kl_value

            # 6.8- 固定代码
            optimizer.zero_grad()
            distll_loss.sum().backward()
            optimizer.step()

            # 6.9- 每隔100个批次或最后一个批次，对学生模型进行验证
            if i%100==0 or i==len(train_dataloader):
                # 6.9.1- 调用评估函数
                f1score, accuracy, precision, recall = eval_model(student_model)
                print(f"第{i}批次，f1score={f1score}，accuracy={accuracy}，precision={precision}，recall={recall}")

                # 6.9.2- 如果验证后发现模型效果有提升（也就是f1score比上次的要大），那就保存模型
                if f1score>best_f1score:
                    torch.save(student_model.state_dict(),config.student_save_model)
                    best_f1score = f1score  # 更新历史最高分

                # 6.9.3- 将模型的模式切回为训练模式
                student_model.train()

if __name__ == '__main__':
    train_and_eval()