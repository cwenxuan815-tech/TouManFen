/** F6：类别典型词（由训练集词频生成；相关词，非因果）。 */
export interface CategoryWordsMeta {
  source: string
  method: string
  sample_size: number
  top_n: number
  note: string
}

export const categoryWordsMeta: CategoryWordsMeta = {
  "source": "00-data/data/train.clean.txt",
  "method": "jieba 分词 + 类别词频（排除停用词/单字/纯数字）",
  "sample_size": 50000,
  "top_n": 8,
  "note": "相关词，非因果；仅作类别解释提示，不代表模型依据。"
}

export const categoryWords: Record<string, string[]> = {
  "finance": [
    "基金",
    "期货",
    "商品",
    "市场",
    "投资",
    "公司",
    "发行",
    "银行"
  ],
  "realty": [
    "均价",
    "开盘",
    "平起",
    "精装",
    "别墅",
    "北京",
    "现房",
    "平米"
  ],
  "stocks": [
    "市场",
    "港股",
    "快讯",
    "震荡",
    "证券",
    "评级",
    "大盘",
    "反弹"
  ],
  "education": [
    "高考",
    "考研",
    "招生",
    "考生",
    "录取",
    "考试",
    "专业",
    "北京"
  ],
  "science": [
    "佳能",
    "手机",
    "索尼",
    "中国",
    "网络",
    "三星",
    "推荐",
    "仅售"
  ],
  "society": [
    "男子",
    "女子",
    "组图",
    "司机",
    "万元",
    "身亡",
    "妻子",
    "学生"
  ],
  "politics": [
    "美国",
    "死亡",
    "总统",
    "日本",
    "发生",
    "朝鲜",
    "组图",
    "奥巴马"
  ],
  "sports": [
    "火箭",
    "中国",
    "曼联",
    "皇马",
    "图文",
    "米兰",
    "巴萨",
    "姚明"
  ],
  "game": [
    "游戏",
    "OL",
    "网游",
    "公布",
    "玩家",
    "世界",
    "活动",
    "开启"
  ],
  "entertainment": [
    "组图",
    "曝光",
    "否认",
    "亮相",
    "回应",
    "男友",
    "明星",
    "香港"
  ]
}
