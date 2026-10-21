import streamlit as st
import requests
import time

# 1- 创建streamlit页面
st.title("投满分项目")
st.write("这是一个投满分项目")

# 2- 获得用户输入
title = st.text_input("请输入新闻标题")

# 3- 发送请求
if st.button("提交"):
    starttime = time.time()

    try:
        # 4- 调用接口
        url = "http://192.168.12.54:8105/predict_api"
        response = requests.post(url, json={"title": title})

        use_time = time.time() - starttime

        # 5- 结果解析，展示到页面
        pred_class = response.json()["pred_class"]
        st.write(f"耗时：{round(use_time, 3)}s")
        st.write(f"预测结果：{pred_class}")
    except Exception as e:
        st.write("网络波动，错误码是：666，请联系人工客服：020-119")
