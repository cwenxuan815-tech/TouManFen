"""fastText worker 入口（薄壳）。"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tmf_common.flask_shell import create_app, run_worker  # noqa: E402

from ft_predict_service import SERVICE  # noqa: E402

app = create_app(SERVICE, import_name=__name__)


if __name__ == "__main__":
    run_worker(app, SERVICE, default_port=8103)
