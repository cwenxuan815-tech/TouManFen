"""随机森林 worker 入口（薄壳）。

职责仅限：加载预测服务 -> 构造 Flask app -> 按配置启动监听。
路由/校验/错误格式由 ``tmf_common.flask_shell`` 与 ``PredictService`` 负责。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tmf_common.flask_shell import create_app, run_worker  # noqa: E402

from rf_predict_service import SERVICE  # noqa: E402

app = create_app(SERVICE, import_name=__name__)


if __name__ == "__main__":
    run_worker(app, SERVICE, default_port=8101)
