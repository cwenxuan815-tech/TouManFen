import os

import requests

if __name__ == '__main__':
    while True:
        # 1- 获取用户输入的新闻标题
        title = input("请输入新闻标题：")
        if title=="exit":
            break

        # 2- 发送请求（默认本地；局域网用 TMF_WORKER_HOST 覆盖）
        worker_host = os.environ.get("TMF_WORKER_HOST", "127.0.0.1")
        url = f"http://{worker_host}:8101/predict_api"
        response = requests.post(url,json={"title":title})

        # 3- 输出响应结果
        print("响应结果是：",response.json())

