from flask import Flask,request,jsonify
from llm_predict_service import predict

# 1- 创建App应用
app = Flask(__name__)

# 2- 创建后端接口
@app.route(rule="/predict_api",methods=["POST"])
def predict_api():
    # 获得用户发送过来的请求
    news_data = request.get_json()

    # 调用预测函数
    result = predict(news_data)

    return jsonify(result)


# ---------------------------------------------------------------------------
# 阶段 2：只读状态端点（不触发预测、不改变既有 /predict_api 行为）
# ---------------------------------------------------------------------------
@app.route(rule="/healthz", methods=["GET"])
def healthz():
    """网关用：仅表示 Flask 进程存活。"""
    return jsonify({"status": "alive", "service": "llm"})


@app.route(rule="/readyz", methods=["GET"])
def readyz():
    """网关用：仅表示本地服务与客户端配置已初始化。

    不验证供应商密钥有效性，不访问外部 LLM API，也不触发计费请求。
    """
    return jsonify({"status": "ready", "model_id": "llm", "provider_check": "not_performed"})


@app.route(rule="/metadata", methods=["GET"])
def metadata():
    """网关用：稳定 model_id/展示名；版本暂无可靠来源，返回 null，不编造。"""
    return jsonify({"model_id": "llm", "display_name": "大模型", "version": None})


if __name__ == '__main__':
    # 3- 启动接口
    app.run(host="192.168.12.54",port=8105,debug=False)