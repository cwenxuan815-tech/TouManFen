import requests

if __name__ == '__main__':
    while True:
        # 1- 获取用户输入的新闻标题
        title = input("请输入新闻标题：")
        if title=="exit":
            break

        # 2- 发送请求
        url = "http://192.168.12.54:8101/predict_api"
        response = requests.post(url,json={"title":title})

        # 3- 输出响应结果
        print("响应结果是：",response.json())

