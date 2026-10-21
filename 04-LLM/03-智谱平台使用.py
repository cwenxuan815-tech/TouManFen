import os
from dotenv import load_dotenv
from openai import OpenAI

# 密钥统一从 04-LLM/.env 读取（不要把 key 写进代码；.env 已在 .gitignore 中）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
)

completion = client.chat.completions.create(
    model="glm-4.7",
    messages=[
        {"role": "system", "content": "你是一个人工智能教授，会写代码，能够讲清楚理论"},
        {"role": "user", "content": "请解释Transformer的架构原理"}
    ],
    top_p=0.7,
    temperature=0.9
)

print(completion.choices[0].message.content)