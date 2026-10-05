import os
import tempfile

# 必须在导入 app.main 之前设置，避免模块级 app 在工作目录创建 claims.db
_tmpdir = tempfile.mkdtemp(prefix="claims-test-")
os.environ["CLAIMS_DB_PATH"] = os.path.join(_tmpdir, "seed.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def order_id(client):
    """2026-09-28 10:00(+08) 创建，承诺 3 个工作日，CN 节假日表。
    9/29、9/30 工作日，10/1-10/7 国庆节假日 -> 承诺截止 2026-10-08 10:00(+08)。"""
    resp = client.post("/v1/orders", json={
        "order_id": "ORD-T-1",
        "created_at": "2026-09-28T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "promise_type": "business_days",
        "promise_value": 3,
        "holiday_calendar": "CN",
    })
    assert resp.status_code == 201, resp.text
    return "ORD-T-1"
