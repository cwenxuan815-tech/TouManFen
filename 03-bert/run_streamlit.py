import os
# 按本文件位置启动 Streamlit 前端（与当前工作目录无关）
_HERE = os.path.dirname(os.path.abspath(__file__))
os.system(f'streamlit run "{os.path.join(_HERE, "app_streamlit.py")}"')
