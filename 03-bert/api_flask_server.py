from flask import Flask,request,jsonify
from bert_predict_service import predict

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
    return jsonify({"status": "alive", "service": "bert"})


@app.route(rule="/readyz", methods=["GET"])
def readyz():
    """网关用：模型在模块导入时已加载，能响应即表示本地服务已初始化。"""
    return jsonify({"status": "ready", "model_id": "bert"})


@app.route(rule="/metadata", methods=["GET"])
def metadata():
    """网关用：稳定 model_id/展示名；版本暂无可靠来源，返回 null，不编造。"""
    return jsonify({"model_id": "bert", "display_name": "BERT", "version": None})


if __name__ == '__main__':
    # 3- 启动接口
    app.run(host="192.168.12.54",port=8104,debug=False)