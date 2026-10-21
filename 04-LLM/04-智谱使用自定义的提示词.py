import os
from dotenv import load_dotenv
from openai import OpenAI

# 密钥统一从 04-LLM/.env 读取（不要把 key 写进代码；.env 已在 .gitignore 中）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
)

# 让模型生成的
system_prompt = """
你是一个专业的新闻分类器。请根据用户提供的新闻标题，将其严格归类到以下10个英文类别中（注意：不要输出中文解释、ID或任何额外文本）：
分类列表（必须二选一）：
finance（金融）、realty（房地产）、stocks（股票/证券）、education（教育）、science（科技）、society（社会）、politics（政治）、sports（体育）、game（游戏）、entertainment（娱乐）
核心规则：
仅输出上述列表中的英文类别名（如 sports），不要输出中文或数字ID。
分类依据必须完全基于标题内容，无需引入外部知识。
严格拒绝输出任何解释、格式说明或多余文本。
关键设计说明：
精准映射类别：
直接使用您提供的英文类别名（与ID顺序完全一致），例如：
finance → ID 0（金融）
realty → ID 1（房地产）
entertainment → ID 9（娱乐）
严格输出限制：
明确要求模型仅输出英文类别名（如 game），避免生成中文或ID。
通过“不要输出中文或数字ID”强化指令，防止模型混淆。
适配中文标题：
支持中文新闻标题的语义分析（如“钢材期货”“海棠公社”），同时兼容特殊符号（如“徐若”）。
分类依据仅依赖标题内容，不依赖外部信息。
大模型优化：
语言简洁（仅3条规则），符合LLM的prompt工程最佳实践，避免冗长导致指令失效。
使用示例：
输入：国务院：严打拐卖操控未成年人违法犯罪
输出：politics
输入：《赤壁OL》攻城战诸侯战硝烟又起
输出：game
输入：82岁老太为学生做饭扫地44年获授港大荣誉院士
输出：society
"""

system_prompt = """
你是一名新闻分类专家，请严格按以下规则执行分类任务：
1. **任务定义**  
基于新闻标题的语义内容，将其精准归类至以下10个类别之一：
`finance, realty, stocks, education, science, society, politics, sports, game, entertainment`
2. **分类规则**  
- **领域关键词匹配**（示例）：  
- `finance/realty/stocks`：含“股价、房贷、汇率、楼市、上市公司”等经济相关术语  
*示例：锌价难续去年辉煌 → finance*  
- `science`：涉及科研/自然现象（如“铁树开花”“电池技术”）  
*示例：60年铁树开花形状似玉米芯 → science*  
- `society`：民生事件/意外事故（如“坠楼”“获救”“纠纷”）  
*示例：2岁男童7楼坠下获救 → society*  
- **多义词消歧**：  
- “金科”在房地产语境→`realty`（如“金科西府 名墅天成”），在游戏语境→`game`  
- “表演”在文体领域→`entertainment`（如“兔子舞热辣表演”），在商业活动→`finance`  
- **体育与游戏区分**：  
- 足球/篮球等实体运动→`sports`（如“FIFA”“布拉特”）  
- 电子游戏/网游→`game`（如“中青宝sg现场抓拍”含游戏公司名）
3. **输出要求**  
- 仅输出**小写英文类别名**，不加标点/解释  
- 无法确定时按标题核心名词归类
4. **示例对照**（供参考，实际提示词中无需包含）  
- 同步A股首秀：港股缩量回调 → `stocks`  
- 布拉特：放球员一条生路 → `sports`  
- 倚天屠龙记十大创新概览 → `game`（电子游戏题材）
"""

news_title = "浙江玻璃取得1亿元人民币合同"
# news_title = "化危为机 推动我国期市创新发展"
# news_title = "调查-您如何评价火箭队签约发展联盟扣篮冠军？"

completion = client.chat.completions.create(
    model="glm-4.7",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": news_title}
    ],
    top_p=0.7,
    temperature=0.9
)

print(completion.choices[0].message.content)