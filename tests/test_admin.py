"""管理后台：提供商 / 模型开关 / 用户封禁。

覆盖的都是「接错线会静默失效」的契约：

- 管理接口只有超级管理员能进（403），匿名 401；
- API key 只返回掩码，更新时留空 = 不修改；
- 禁用模型/提供商后，`/api/models` 与对话白名单**同时**生效；
- 同步上游是 upsert，不会覆盖管理员调过的启用状态；
- 封禁会立刻删掉会话（不是等 cookie 过期），且不能封自己/其他管理员。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import create_db_and_tables
from app.schemas.chat import ChatMessage, ChatRequest
from app.server import create_app
from app.services import model_service
from app.services.auth import promote_configured_superusers

PASSWORD = "hunter2hunter2"


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def register(client: TestClient, email: str) -> str:
    response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", data={"username": email, "password": PASSWORD})
    assert response.status_code == 204, response.text


def login_expecting_failure(client: TestClient, email: str) -> int:
    return client.post("/api/auth/login", data={"username": email, "password": PASSWORD}).status_code


def make_admin(client: TestClient, monkeypatch) -> str:
    email = unique_email("admin")
    monkeypatch.setattr(settings, "superuser_emails", email)
    register(client, email)
    login(client, email)
    return email


def create_provider(client: TestClient, *, name: str = "测试提供商", api_key: str | None = "sk-test-1234567890"):
    response = client.post(
        "/api/admin/providers",
        json={"name": name, "base_url": "http://127.0.0.1:9999/v1", "api_key": api_key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_model(client: TestClient, provider_id: str, *, model_id: str = "test-chat", **extra):
    payload = {"model_id": model_id, **extra}
    response = client.post(f"/api/admin/providers/{provider_id}/models", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------
def test_admin_requires_superuser(client, monkeypatch):
    assert client.get("/api/admin/providers").status_code == 401

    email = unique_email()
    register(client, email)
    login(client, email)

    assert client.get("/api/admin/providers").status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_superuser_is_granted_by_configured_email(client, monkeypatch):
    email = make_admin(client, monkeypatch)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["is_superuser"] is True
    assert me.json()["email"] == email


def test_startup_promotion_is_idempotent(monkeypatch):
    import asyncio

    email = unique_email("late-admin")
    with TestClient(create_app()) as client:
        register(client, email)

    monkeypatch.setattr(settings, "superuser_emails", email)
    assert asyncio.run(promote_configured_superusers()) == 1
    assert asyncio.run(promote_configured_superusers()) == 0

    with TestClient(create_app()) as client:
        login(client, email)
        assert client.get("/api/admin/providers").status_code == 200


# ---------------------------------------------------------------------
# 提供商
# ---------------------------------------------------------------------
def test_provider_crud_never_returns_the_raw_api_key(client, monkeypatch):
    make_admin(client, monkeypatch)

    provider = create_provider(client, api_key="sk-super-secret-value")
    assert provider["api_key_hint"] == "sk-…alue"
    assert "sk-super-secret-value" not in str(provider)
    assert provider["slug"]

    # 不带 api_key 的更新不能把密钥清掉
    updated = client.patch(
        f"/api/admin/providers/{provider['id']}",
        json={"name": "改名后的提供商"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "改名后的提供商"
    assert updated.json()["api_key_hint"] == "sk-…alue"

    # 显式空串才清空
    cleared = client.patch(f"/api/admin/providers/{provider['id']}", json={"api_key": ""})
    assert cleared.status_code == 200
    assert cleared.json()["api_key_hint"] is None

    assert client.delete(f"/api/admin/providers/{provider['id']}").status_code == 204
    assert client.get("/api/admin/providers").json() == []


def test_provider_rejects_non_http_base_url(client, monkeypatch):
    make_admin(client, monkeypatch)

    response = client.post(
        "/api/admin/providers",
        json={"name": "坏地址", "base_url": "file:///etc/passwd"},
    )
    assert response.status_code == 422


def test_provider_delete_cascades_models(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    model = create_model(client, provider["id"])

    assert client.get("/api/models").json()["models"][0]["id"] == model["key"]
    assert client.delete(f"/api/admin/providers/{provider['id']}").status_code == 204
    assert client.get("/api/models").json() == {"models": [], "default_model": ""}


# ---------------------------------------------------------------------
# 模型开关
# ---------------------------------------------------------------------
def test_disabled_model_disappears_from_list_and_chat_whitelist(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    model = create_model(client, provider["id"], is_default=True)

    listed = client.get("/api/models").json()
    assert [item["id"] for item in listed["models"]] == [model["key"]]
    assert listed["default_model"] == model["key"]

    assert client.patch(f"/api/admin/models/{model['id']}", json={"enabled": False}).status_code == 200
    assert client.get("/api/models").json() == {"models": [], "default_model": ""}

    chat = client.post(
        "/api/chat",
        json={"model": model["key"], "messages": [{"role": "user", "content": "你好"}]},
    )
    assert chat.status_code == 400
    assert "不在可用模型列表" in chat.json()["detail"]


def test_disabled_provider_hides_its_models(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    create_model(client, provider["id"], is_default=True)

    client.patch(f"/api/admin/providers/{provider['id']}", json={"enabled": False})
    assert client.get("/api/models").json() == {"models": [], "default_model": ""}


def test_no_enabled_model_returns_503(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    model = create_model(client, provider["id"], is_default=True)

    client.patch(f"/api/admin/models/{model['id']}", json={"enabled": False})

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 503
    assert "尚未启用任何模型" in response.json()["detail"]


def test_only_one_default_model(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    first = create_model(client, provider["id"], model_id="first", is_default=True)
    second = create_model(client, provider["id"], model_id="second", is_default=True)

    models = client.get(f"/api/admin/providers/{provider['id']}/models").json()
    defaults = {item["key"]: item["is_default"] for item in models}
    assert defaults[second["key"]] is True
    assert defaults[first["key"]] is False


def test_model_key_is_provider_scoped(client, monkeypatch):
    """两个提供商提供同名模型时必须得到两个不同的路由键。"""
    make_admin(client, monkeypatch)
    one = create_provider(client, name="提供商一")
    two = create_provider(client, name="提供商二")
    first = create_model(client, one["id"], model_id="gpt-4o", is_default=True)
    second = create_model(client, two["id"], model_id="gpt-4o")

    assert first["key"] != second["key"]
    keys = {item["id"] for item in client.get("/api/models").json()["models"]}
    assert keys == {first["key"], second["key"]}


def test_duplicate_model_id_is_rejected(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    create_model(client, provider["id"], model_id="same")

    response = client.post(
        f"/api/admin/providers/{provider['id']}/models",
        json={"model_id": "same"},
    )
    assert response.status_code == 409


def test_disabled_model_cannot_be_made_default(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    model = create_model(client, provider["id"], enabled=False)

    response = client.patch(f"/api/admin/models/{model['id']}", json={"is_default": True})
    assert response.status_code == 400
    assert "停用" in response.json()["detail"]


# ---------------------------------------------------------------------
# 同步上游
# ---------------------------------------------------------------------
class FakeProviderResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeProviderClient:
    def __init__(self, response: FakeProviderResponse):
        self.response = response
        self.calls: list[dict] = []

    async def get(self, url, headers=None, timeout=None):  # noqa: ANN001, ARG002
        self.calls.append({"url": url, "headers": headers or {}})
        return self.response


def _gateway_payload():
    return {
        "data": [
            {"id": "vendor/model-a", "display_name": "Model A", "context_window": 128000},
            {"id": "vendor/model-b", "display_name": "Model B", "reasoning": True},
        ]
    }


def test_sync_upserts_and_preserves_enabled_state(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)

    fake = FakeProviderClient(FakeProviderResponse(payload=_gateway_payload()))
    monkeypatch.setattr(model_service, "get_http_client", lambda: fake)

    first = client.post(f"/api/admin/providers/{provider['id']}/sync")
    assert first.status_code == 200, first.text
    assert first.json() == {"fetched": 2, "created": 2, "updated": 0}
    assert fake.calls[0]["url"] == "http://127.0.0.1:9999/v1/models"

    models = client.get(f"/api/admin/providers/{provider['id']}/models").json()
    disabled = next(item for item in models if item["model_id"] == "vendor/model-b")
    client.patch(f"/api/admin/models/{disabled['id']}", json={"enabled": False})

    second = client.post(f"/api/admin/providers/{provider['id']}/sync")
    assert second.json() == {"fetched": 2, "created": 0, "updated": 2}

    models = client.get(f"/api/admin/providers/{provider['id']}/models").json()
    assert len(models) == 2
    assert next(item for item in models if item["model_id"] == "vendor/model-b")["enabled"] is False


def test_test_connection_reports_failure_without_5xx(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client, api_key="sk-bad")

    fake = FakeProviderClient(FakeProviderResponse(status_code=401))
    monkeypatch.setattr(model_service, "get_http_client", lambda: fake)

    result = client.post(f"/api/admin/providers/{provider['id']}/test")
    assert result.status_code == 200
    assert result.json()["ok"] is False
    assert "API Key" in result.json()["error"]

    sync = client.post(f"/api/admin/providers/{provider['id']}/sync")
    assert sync.status_code == 502


# ---------------------------------------------------------------------
# 用户封禁
# ---------------------------------------------------------------------
def test_ban_kills_existing_session_and_blocks_login(client, monkeypatch):
    make_admin(client, monkeypatch)

    victim_email = unique_email("victim")
    victim_id = register(client, victim_email)

    with TestClient(create_app()) as victim:
        login(victim, victim_email)
        assert victim.get("/api/auth/me").status_code == 200

        banned = client.post(f"/api/admin/users/{victim_id}/ban")
        assert banned.status_code == 200
        assert banned.json()["is_active"] is False

        # 已有会话立刻失效（token 被删），而不是等 cookie 过期
        assert victim.get("/api/auth/me").status_code == 401
        assert login_expecting_failure(victim, victim_email) == 400

    unbanned = client.post(f"/api/admin/users/{victim_id}/unban")
    assert unbanned.status_code == 200
    assert unbanned.json()["is_active"] is True
    login(client, victim_email)


def test_cannot_ban_self_or_another_superuser(client, monkeypatch):
    admin_email = make_admin(client, monkeypatch)
    admin_id = client.get("/api/auth/me").json()["id"]

    self_ban = client.post(f"/api/admin/users/{admin_id}/ban")
    assert self_ban.status_code == 400
    assert "当前登录" in self_ban.json()["detail"]

    other_email = unique_email("admin2")
    monkeypatch.setattr(settings, "superuser_emails", f"{admin_email},{other_email}")
    other_id = register(client, other_email)
    other_ban = client.post(f"/api/admin/users/{other_id}/ban")
    assert other_ban.status_code == 400
    assert "超级管理员" in other_ban.json()["detail"]


def test_user_list_supports_search_and_pagination(client, monkeypatch):
    make_admin(client, monkeypatch)
    prefix = f"search-{uuid.uuid4().hex[:8]}"
    for index in range(3):
        register(client, f"{prefix}-{index}@example.com")

    page = client.get("/api/admin/users", params={"query": prefix, "limit": 2, "offset": 0})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    second = client.get("/api/admin/users", params={"query": prefix, "limit": 2, "offset": 2})
    assert len(second.json()["items"]) == 1


# ---------------------------------------------------------------------
# 环境变量引导
# ---------------------------------------------------------------------
async def test_seed_default_provider_is_idempotent(monkeypatch):
    await create_db_and_tables()
    monkeypatch.setattr(settings, "bootstrap_provider_from_env", True)
    monkeypatch.setattr(settings, "litellm_api_base", "http://gateway.local:4000")
    monkeypatch.setattr(settings, "litellm_api_key", "sk-bootstrap")
    monkeypatch.setattr(settings, "model_name", "openai/gpt-4o")

    assert await model_service.seed_default_provider() is True
    assert await model_service.seed_default_provider() is False

    providers = await model_service.list_providers()
    assert len(providers) == 1
    assert providers[0].base_url == "http://gateway.local:4000"
    assert providers[0].api_key == "sk-bootstrap"

    listed = await model_service.fetch_available_models()
    assert listed.default_model.endswith("/openai/gpt-4o")
    assert listed.models[0].name == "gpt-4o"


def test_chat_request_schema_accepts_provider_scoped_key(client, monkeypatch):
    """路由键里的 `/` 不该被请求模型校验误伤。"""
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    model = create_model(client, provider["id"], is_default=True)

    request = ChatRequest(model=model["key"], messages=[ChatMessage(role="user", content="你好")])
    assert request.model == model["key"]
