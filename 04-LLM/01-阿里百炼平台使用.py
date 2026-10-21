import os
from dotenv import load_dotenv
from openai import OpenAI

# 密钥统一从 04-LLM/.env 读取（不要把 key 写进代码；.env 已在 .gitignore 中）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
client = OpenAI(
    # 若没有配置环境变量，请用百炼API Key将下行替换为：api_key="sk-xxx"
    api_key=os.getenv("QWEN_API_KEY"),
    base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
)

completion = client.chat.completions.create(
    # 模型列表：https://help.aliyun.com/zh/model-studio/getting-started/models
    model="qwen-coder-plus-latest",
    messages=[
        {"role": "system", "content": "你是一个人工智能教授，会写代码，能够讲清楚理论"},
        {"role": "user", "content": "请解释Transformer的架构原理"},
    ]
)
print(completion.model_dump_json())