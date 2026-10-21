# pip install flask
from flask import Flask,request,jsonify
from rf_predict_service import predict

# 1- 创建Application应用对象
app = Flask(__name__)

# API application process interface：URL地址，交换数据
# 2- 创建预测的后端API接口
"""
    POST和GET的区别：
        1- 安全性：POST相对GET要安全些
        2- 数据量：POST传输的数据量比GET要多
        3- 参数位置：GET在URL的后面，POST参数在表单中
"""
@app.route(rule="/predict_api",methods=["POST"])
def predict_api():
    # 1- 获取用户输入的参数信息
    news_data = request.get_json()
    # print(f"用户发送过来的请求参数内容：{news_data}，类型是：{type(news_data)}")

    # 2- 调用模型预测方法
    result = predict(news_data)

    # 3- 返回预测结果给用户：将Python中的字典转成JSON字符串返回给用户
    return jsonify(result)


# ---------------------------------------------------------------------------
# 阶段 2：只读状态端点（不触发预测、不改变既有 /predict_api 行为）
# ---------------------------------------------------------------------------
@app.route(rule="/healthz", methods=["GET"])
def healthz():
    """网关用：仅表示 Flask 进程存活。"""
    return jsonify({"status": "alive", "service": "rf"})


@app.route(rule="/readyz", methods=["GET"])
def readyz():
    """网关用：模型在模块导入时已加载，能响应即表示本地服务已初始化。"""
    return jsonify({"status": "ready", "model_id": "rf"})


@app.route(rule="/metadata", methods=["GET"])
def metadata():
    """网关用：稳定 model_id/展示名；版本暂无可靠来源，返回 null，不编造。"""
    return jsonify({"model_id": "rf", "display_name": "随机森林", "version": None})


if __name__ == '__main__':
    # 启动后端程序
    """
       参数解释：
            host：程序运行的服务器IP地址
            port：程序绑定到服务器的什么端口号上。推荐设置范围是1024-65535之间 
    """
    app.run(host="192.168.12.54",port=8101,debug=False)