# pip install streamlit
import os
import streamlit as st
import requests
import time

if __name__ == '__main__':
    # 1- 创建页面
    st.title("投满分项目")
    st.write("这是一个投满分项目")

    # 2- 获取用户输入的内容
    title = st.text_input("请输入新闻标题")

    # 3- 发送
    worker_host = os.environ.get("TMF_WORKER_HOST", "127.0.0.1")
    url = f"http://{worker_host}:8101/predict_api"
    if st.button("提交"):
        start_time = time.time()

        try:
            response = requests.post(url, json={"title": title})
            result = response.json()["pred_class"]
            use_time = time.time() - start_time

            st.write(f"耗时：{round(use_time, 3)}s")
            st.write(f"新闻分类预测结果是：{result}")
        except Exception as e:
            st.write("网络波动，错误码是：666，请联系人工客服：020-119")