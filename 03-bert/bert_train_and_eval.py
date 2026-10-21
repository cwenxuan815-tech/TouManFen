import os
import time

from config import Config
from log import Logger
from data_preprocessing import build_dataloader
from bert_model import BertClassifierModel
import torch
import torch.nn as nn
from tqdm import tqdm
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

config = Config()

# 训练日志：同时打印到控制台并追加写入文件（长时间运行时可 tail 观察）
os.makedirs(os.path.dirname(config.train_log), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(config.train_log, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt_hms(seconds):
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s"


def eval_model(model):
    # 1- 加载验证集数据
    dev_dataloader = build_dataloader(datapath=config.dev_datapath, shuffle=False)

    # 2- 模型评估
    all_pred_result = []    # 预测结果列表
    all_true_result = []    # 真实结果列表

    model.eval()
    with torch.no_grad():
        for i, batch in enumerate(dev_dataloader, start=1):
            input_ids, attention_mask, labels = batch
            input_ids = input_ids.to(config.device)
            attention_mask = attention_mask.to(config.device)
            labels = labels.to(config.device)

            pred_output = model(input_ids, attention_mask)
            pred_index = torch.argmax(pred_output, dim=-1)
            all_pred_result.extend(pred_index.cpu().tolist())
            all_true_result.extend(labels.cpu().tolist())

    # 3- 计算评估指标
    f1score = f1_score(all_true_result, all_pred_result, average="macro")
    accuracy = accuracy_score(all_true_result, all_pred_result)
    precision = precision_score(all_true_result, all_pred_result, average="macro")
    recall = recall_score(all_true_result, all_pred_result, average="macro")

    return f1score, accuracy, precision, recall


def train_and_eval():
    # 1- 加载数据
    train_dataloader = build_dataloader(datapath=config.train_datapath, shuffle=True)
    total_steps = len(train_dataloader) * config.num_epochs

    # 2- 创建模型
    model = BertClassifierModel().to(device=config.device)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log(f"训练开始 | 设备={config.device} | 冻结编码器={config.freeze_bert} | 可训练参数={trainable/1e6:.1f}M")
    log(f"         | epochs={config.num_epochs} lr={config.learning_rate} batch={config.batch_size} "
        f"max_length={config.max_length} | 每epoch {len(train_dataloader)} 步，合计 {total_steps} 步")
    log(f"         | 每 {config.eval_interval} 步验证一次 | 训练集={config.train_datapath}")

    # 3- 损失函数对象
    loss = nn.CrossEntropyLoss()

    # 4- 优化器对象
    optimizer = torch.optim.AdamW(params=model.parameters(), lr=config.learning_rate)

    # 5- 其他变量
    best_f1score = 0.0        # f1score历史最高分
    global_step = 0
    start_epoch = 0
    t_start = time.time()

    # 5.1- 断点续训（TMF_RESUME=checkpoint 路径）：用于先跑 1 epoch、再决定是否续跑第 2 epoch
    resume_path = os.environ.get("TMF_RESUME", "")
    if resume_path and os.path.exists(resume_path):
        ckpt = torch.load(resume_path, map_location=config.device)
        model.load_state_dict(ckpt["model"])
        optimizer.load_state_dict(ckpt["optimizer"])
        start_epoch = ckpt.get("epoch", 0)
        global_step = ckpt.get("global_step", 0)
        best_f1score = ckpt.get("best_f1", 0.0)
        log(f"** 断点续训：载入 {resume_path}（已完成 epoch={start_epoch}, step={global_step}, best_f1={best_f1score:.4f}）")

    # 6- 训练
    model.train()
    run_step = 0          # 本次运行内已完成的步数（用于 ETA）
    for epoch in range(config.num_epochs):
        epoch_no = start_epoch + epoch + 1
        for i, batch in enumerate(tqdm(train_dataloader), start=1):
            # 6.2- 将数据发送到对应的设备
            input_ids, attention_mask, labels = batch
            input_ids = input_ids.to(config.device)
            attention_mask = attention_mask.to(config.device)
            labels = labels.to(config.device)

            # 6.3- 前向传播
            pred_output = model(input_ids, attention_mask)

            # 6.4- 算损失值
            loss_value = loss(pred_output, labels)

            # 6.5- 反向传播 + 更新
            optimizer.zero_grad()
            loss_value.backward()
            optimizer.step()

            global_step += 1
            run_step += 1

            # 6.6- 打印进度（含 ETA）
            if global_step % config.log_interval == 0 or run_step == total_steps:
                elapsed = time.time() - t_start
                speed = elapsed / run_step
                eta = speed * (total_steps - run_step)
                log(f"epoch {epoch_no}/{start_epoch + config.num_epochs} | step {global_step} "
                    f"(本次 {run_step}/{total_steps}) | loss={loss_value.item():.4f} | {speed:.2f}s/step "
                    f"| 已用 {fmt_hms(elapsed)} | ETA {fmt_hms(eta)}")

            # 6.7- 每隔 eval_interval 个批次（或最后一步）在 dev 上验证
            if global_step % config.eval_interval == 0 or run_step == total_steps:
                t_eval = time.time()
                f1score, accuracy, precision, recall = eval_model(model)
                log(f"** 验证 @step {global_step} | f1={f1score:.4f} acc={accuracy:.4f} "
                    f"p={precision:.4f} r={recall:.4f} | 评估耗时 {fmt_hms(time.time() - t_eval)}")

                # 效果提升则保存模型
                if f1score > best_f1score:
                    best_f1score = f1score
                    torch.save(model.state_dict(), config.save_model)
                    log(f"** 保存模型（最佳 f1={best_f1score:.4f}）-> {config.save_model}")

                model.train()

        # 6.8- 每个 epoch 结束时保存断点（含优化器状态），便于之后再续跑
        ckpt_path = os.path.join(os.path.dirname(config.save_model), "checkpoint.pt")
        torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                    "epoch": epoch_no, "global_step": global_step, "best_f1": best_f1score}, ckpt_path)
        log(f"** 已保存断点（epoch={epoch_no}, step={global_step}）-> {ckpt_path}")

    log(f"训练结束 | 最佳 f1={best_f1score:.4f} | 总用时 {fmt_hms(time.time() - t_start)} | 模型：{config.save_model}")


if __name__ == '__main__':
    train_and_eval()
