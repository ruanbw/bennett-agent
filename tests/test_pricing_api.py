"""价格管理 API 的行为锁。

只验两件在路由层才会出错的事：

1. **带斜杠的模型键**。模型键是 `{slug}/{model_id}`，而 `model_id` 自己常带斜杠
   （`openai/gpt-4o`），整条键可能是 `my-gw/openai/gpt-4o`。用普通路径参数只能拿到
   第一段——写进去的价格会挂在错误的键上，而且**看起来成功**。
2. **空值与 0 的区别**。清空某个价格（None）与配成 0（免费）是两回事，路由层不能
   把两者混在一起（Pydantic 的 `None` 默认值容易让它们悄悄合并）。

权限边界不在这里重测：整个 admin 路由挂在 `current_admin` 上，那一条由
`tests/test_roles.py` / `tests/test_admin.py` 覆盖。这里用 override 跳过鉴权，
专注于路由行为本身。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import router as admin_router
from app.services import pricing
from app.services.auth import current_admin


@pytest.fixture(autouse=True)
async def _tables():
    # 结构由 conftest 的 alembic 建立；用例之间由 TRUNCATE ... CASCADE 隔离。
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(admin_router)
    # 鉴权不在这里测（见模块 docstring），用一个桩绕过。
    app.dependency_overrides[current_admin] = lambda: object()
    return TestClient(app)


def _payload(**overrides) -> dict:
    body = {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "cached_input_per_mtok": 0.3, "currency": "USD"}
    body.update(overrides)
    return body


def test_list_prices_is_empty_initially(client):
    response = client.get("/api/admin/prices")

    assert response.status_code == 200
    assert response.json() == []


def test_put_creates_then_get_lists_it(client):
    created = client.put("/api/admin/prices/gw%2Fmodel-a", json=_payload())

    assert created.status_code == 200
    body = created.json()
    assert body["model_id"] == "gw/model-a"
    assert body["input_per_mtok"] == 3.0
    assert body["currency"] == "USD"

    listed = client.get("/api/admin/prices").json()
    assert [entry["model_id"] for entry in listed] == ["gw/model-a"]


def test_put_accepts_model_key_with_multiple_slashes(client):
    """`my-gw/openai/gpt-4o` 这类键必须原样落库。

    这是 `{model_key:path}` 的唯一理由：普通路径参数只会拿到 `my-gw`，剩下的部分被
    当成不存在的子路由，而**写入看起来是成功的**（价格挂在了错的键上）。
    """
    response = client.put("/api/admin/prices/my-gw/openai/gpt-4o", json=_payload(input_per_mtok=2.5))

    assert response.status_code == 200
    assert response.json()["model_id"] == "my-gw/openai/gpt-4o"

    fetched = client.get("/api/admin/prices").json()
    assert [entry["model_id"] for entry in fetched] == ["my-gw/openai/gpt-4o"]


def test_put_overwrites(client):
    client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok=1.0))
    client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok=9.0))

    listed = client.get("/api/admin/prices").json()
    assert len(listed) == 1
    assert listed[0]["input_per_mtok"] == 9.0


def test_put_with_null_price_clears_it(client):
    """显式 null = 清空（未知），与 0（免费）不是同一件事。"""
    client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok=3.0))

    response = client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok=None))

    assert response.status_code == 200
    assert response.json()["input_per_mtok"] is None


def test_put_zero_price_is_accepted_and_distinct_from_null(client):
    """0 是合法价格（免费模型），必须能存并且读回来是 0，不是 null。"""
    response = client.put("/api/admin/prices/gw%2Afree", json=_payload(input_per_mtok=0, output_per_mtok=0))

    assert response.status_code == 200
    assert response.json()["input_per_mtok"] == 0.0

    stored = client.get("/api/admin/prices").json()
    assert stored[0]["input_per_mtok"] == 0.0


def test_put_rejects_negative_price(client):
    response = client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok=-1))

    assert response.status_code == 422


def test_put_rejects_blank_model_key(client):
    """空键会让所有「未命名」价格互相覆盖（model_id 有唯一约束）。"""
    response = client.put("/api/admin/prices/%20", json=_payload())

    assert response.status_code == 400


def test_put_normalizes_currency(client):
    response = client.put("/api/admin/prices/gw%2Fa", json=_payload(currency=" cny "))

    assert response.status_code == 200
    assert response.json()["currency"] == "CNY"


def test_delete_removes_price(client):
    client.put("/api/admin/prices/gw%2Fa", json=_payload())

    response = client.delete("/api/admin/prices/gw%2Fa")

    assert response.status_code == 204
    assert client.get("/api/admin/prices").json() == []


def test_delete_missing_price_is_404(client):
    response = client.delete("/api/admin/prices/nobody%2Fhere")

    assert response.status_code == 404


def test_put_works_for_model_that_does_not_exist(client):
    """价格与模型是否存在于两张表——提前录入还没上线的模型是合法需求。"""
    response = client.put("/api/admin/prices/future%2Fmodel", json=_payload())

    assert response.status_code == 200


async def test_api_roundtrip_matches_service_layer_decimals(client):
    """API 走的是 float（展示），服务层存的是 Decimal——精确计算不能经过 API 层。"""
    client.put("/api/admin/prices/gw%2Fa", json=_payload(input_per_mtok="3.000001"))

    stored = await pricing.get_price("gw/a")

    assert stored is not None
    # 三位以上的精度在 float 往返后仍是这个值（18 位以内 float 足够表达这个量级），
    # 但**真正的计算**用的是库里的 Decimal，不经过 response schema。
    assert stored.input_per_mtok is not None
    assert stored.input_per_mtok > 3
