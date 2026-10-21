import os
from dotenv import load_dotenv
from openai import OpenAI

# 密钥统一从 04-LLM/.env 读取（不要把 key 写进代码；.env 已在 .gitignore 中）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "你是一个人工智能教授，会写代码，能够讲清楚理论"},
        {"role": "user", "content": "请解释Transformer的架构原理"},
    ],
    stream=False
)

print(response.choices[0].message.content)