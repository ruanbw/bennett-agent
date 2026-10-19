"""管理后台：提供商 / 模型开关 / 用户封禁。

覆盖的都是「接错线会静默失效」的契约：

- 管理接口需要 `role >= admin`（普通用户 403），匿名 401；
- API key 只返回掩码，更新时留空 = 不修改；
- 禁用模型/提供商后，`/api/models` 与对话白名单**同时**生效；
- 同步上游是 upsert，不会覆盖管理员调过的启用状态；
- 封禁会立刻删掉会话（不是等 cookie 过期），且不能封自己/同级或更高等级。

角色相关的完整链路（升/降级、踢会话、最后一个超管的守卫）在 `tests/test_roles.py`。
"""

import asyncio
import socket
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import create_db_and_tables
from app.roles import Role
from app.schemas.chat import ChatMessage, ChatRequest
from app.server import create_app
from app.services import model_service
from tests.accounts import set_role_in_db

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


def make_admin(client: TestClient, _monkeypatch=None) -> str:
    """造一个已登录的 admin，返回 email。第二个参数已用不上，保留只为调用点签名一致。"""
    email = unique_email("admin")
    register(client, email)
    asyncio.run(set_role_in_db(email, Role.ADMIN))
    login(client, email)
    return email


#: 提供商出站目标的默认值。用 IP 字面量而不是域名：守卫会解析主机名，用例不该依赖
#: DNS；用公网 IP 而不是环回/私网，因为那些地址在默认模式下本就放行，代表不了
#: 「正常目标」。
PROVIDER_BASE_URL = "http://93.184.216.34:9999/v1"


def create_provider(
    client: TestClient,
    *,
    name: str = "测试提供商",
    base_url: str = PROVIDER_BASE_URL,
    api_key: str | None = "sk-test-1234567890",
):
    response = client.post(
        "/api/admin/providers",
        json={"name": name, "base_url": base_url, "api_key": api_key},
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
def test_admin_endpoints_require_admin_role(client, monkeypatch):
    """匿名 401，登录但 role < admin 则 403。"""
    assert client.get("/api/admin/providers").status_code == 401

    email = unique_email()
    register(client, email)
    login(client, email)

    assert client.get("/api/admin/providers").status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_admin_can_reach_the_console_once_its_role_is_set(client):
    """写库后即生效：`/api/auth/me` 的 `role` / `is_superuser` 与库一致，且能真进后台。"""
    email = make_admin(client)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert me.json()["role"] == "admin"
    # is_superuser 是 role 的派生镜像（apply_role 保持同步），admin 即为 True
    assert me.json()["is_superuser"] is True
    assert client.get("/api/admin/providers").status_code == 200


def test_startup_never_promotes_anyone(client):
    """启动流程不得改任何人的角色：连续两次启动后，一个普通账号仍然是普通用户。"""
    email = unique_email("never-admin")
    register(client, email)

    # 再启动两次应用（lifespan 每次都跑一遍）
    for _ in range(2):
        with TestClient(create_app()):
            pass

    login(client, email)
    assert client.get("/api/auth/me").json()["role"] == "user"
    assert client.get("/api/admin/providers").status_code == 403


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
    assert fake.calls[0]["url"] == f"{PROVIDER_BASE_URL}/models"

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
# 换地址即换凭据：PATCH base_url 必须重填 API Key
# ---------------------------------------------------------------------
def test_changing_base_url_without_a_new_api_key_is_rejected(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client, api_key="sk-super-secret-value")

    rejected = client.patch(
        f"/api/admin/providers/{provider['id']}",
        json={"base_url": "https://api.deepseek.com/v1"},
    )
    assert rejected.status_code == 400
    assert "API Key" in rejected.json()["detail"]

    # 被拒的更新不能留下半截状态：地址与密钥都没变
    unchanged = client.get("/api/admin/providers").json()[0]
    assert unchanged["base_url"] == PROVIDER_BASE_URL
    assert unchanged["api_key_hint"] == provider["api_key_hint"]

    # 重填了 key 就放行
    accepted = client.patch(
        f"/api/admin/providers/{provider['id']}",
        json={"base_url": "https://api.deepseek.com/v1", "api_key": "sk-brand-new-1234"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["base_url"] == "https://api.deepseek.com/v1"
    assert accepted.json()["api_key_hint"] == "sk-…1234"


def test_resubmitting_the_same_base_url_still_allows_an_empty_api_key(client, monkeypatch):
    """前端每次保存都回传 base_url：地址没变时不能把 key 变成必填。"""
    make_admin(client, monkeypatch)
    provider = create_provider(client, api_key="sk-super-secret-value")

    updated = client.patch(
        f"/api/admin/providers/{provider['id']}",
        json={"name": "改名", "base_url": PROVIDER_BASE_URL, "enabled": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名"
    assert updated.json()["enabled"] is False
    assert updated.json()["api_key_hint"] == "sk-…alue"


def test_clearing_the_api_key_while_changing_base_url_leaves_nothing_to_leak(client, monkeypatch):
    """显式清空 = 没有凭据可外带，因此不必强制重填。"""
    make_admin(client, monkeypatch)
    provider = create_provider(client, api_key="sk-super-secret-value")

    updated = client.patch(
        f"/api/admin/providers/{provider['id']}",
        json={"base_url": "https://api.deepseek.com/v1", "api_key": ""},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["api_key_hint"] is None


# ---------------------------------------------------------------------
# 出站目标地址检查（API Key 不能送到元数据服务／任意主机）
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "base_url",
    [
        # 元数据族：任何配置下都拒绝（169.254.169.254 是云厂商实例元数据服务）
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0:9999/v1",
        "http://[fe80::1]:9999/v1",
    ],
)
async def test_metadata_targets_are_always_rejected(base_url):
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service._reject_private_target(base_url)
    assert "链路本地" in str(excinfo.value)


@pytest.mark.parametrize(
    "base_url",
    [
        # 文档里最主要的用法：本机 LiteLLM 网关 / 内网自建 vLLM
        "http://127.0.0.1:3425/v1",
        "http://localhost:4000/v1",
        "http://10.0.0.1:8000/v1",
        "http://172.16.5.4/v1",
        "http://192.168.1.10/v1",
        "http://[::1]:9999/v1",
    ],
)
async def test_loopback_and_private_targets_are_allowed_by_default(base_url):
    await model_service._reject_private_target(base_url)


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:3425/v1", "http://localhost:4000/v1", "http://10.0.0.1:8000/v1"],
)
async def test_private_targets_are_rejected_in_strict_mode(monkeypatch, base_url):
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service._reject_private_target(base_url)
    assert "内网" in str(excinfo.value)


def _public_getaddrinfo(host, port):  # noqa: ANN001, ARG001
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


@pytest.mark.parametrize(
    "base_url",
    ["https://api.deepseek.com/v1", "http://8.8.8.8:9999/v1", "http://93.184.216.34/v1"],
)
async def test_public_targets_are_allowed(monkeypatch, base_url):
    # 公网域名不该真的去解析：用例只关心这条规则是否放行
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)
    await model_service._reject_private_target(base_url)


async def test_dns_failure_is_not_treated_as_an_internal_target(monkeypatch):
    def _unresolvable(host, port):  # noqa: ANN001, ARG001
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(model_service.socket, "getaddrinfo", _unresolvable)
    # 不抛：解析失败交给后面的连接错误，而不是在这里报「内网」
    await model_service._reject_private_target("https://provider.invalid/v1")


def test_metadata_target_is_refused_at_creation(client, monkeypatch):
    """写入时就拒：管理员当场看到 400 与原因，而不是存进去等某次对话才静默失败。"""
    make_admin(client, monkeypatch)

    response = client.post(
        "/api/admin/providers",
        json={"name": "元数据服务", "base_url": "http://169.254.169.254/latest/meta-data", "api_key": "sk-x"},
    )

    assert response.status_code == 400
    assert "链路本地" in response.json()["detail"]


def test_metadata_target_never_gets_the_stored_key(client, monkeypatch):
    """端到端：库里已经存着明文 key（守卫上线前写入的历史行），也不发出请求。

    写入路径现在会拒（见上一个用例），但库里可能有守卫上线前就存在的行，或者写入时
    严格模式还是关的。这条锁的是「即使真进了库，`test` / `sync` 也不会把 key 送出去」。
    """
    make_admin(client, monkeypatch)
    provider_id = asyncio.run(
        _insert_provider_directly(
            name="历史遗留", base_url="http://169.254.169.254/latest/meta-data", api_key="sk-super-secret-value"
        )
    )

    result = client.post(f"/api/admin/providers/{provider_id}/test")
    assert result.status_code == 200
    assert result.json()["ok"] is False
    assert "链路本地" in result.json()["error"]

    sync = client.post(f"/api/admin/providers/{provider_id}/sync")
    assert sync.status_code == 502
    assert "链路本地" in sync.json()["detail"]


async def _insert_provider_directly(*, name: str, base_url: str, api_key: str) -> str:
    """绕过 API 与守卫，直接写一行提供商——模拟守卫上线前就已存在的配置。"""
    from app.db import ModelProvider, async_session_maker

    async with async_session_maker() as session:
        provider = ModelProvider(name=name, slug=f"legacy-{uuid.uuid4().hex[:8]}", base_url=base_url, api_key=api_key)
        session.add(provider)
        await session.commit()
        return str(provider.id)


def test_public_target_reaches_the_provider(client, monkeypatch):
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    fake = FakeProviderClient(FakeProviderResponse(payload=_gateway_payload()))
    monkeypatch.setattr(model_service, "get_http_client", lambda: fake)

    result = client.post(f"/api/admin/providers/{provider['id']}/test")
    assert result.status_code == 200
    assert result.json() == {"ok": True, "model_count": 2, "error": None}


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


def test_cannot_ban_self_or_a_higher_role(client, monkeypatch):
    make_admin(client)
    admin_id = client.get("/api/auth/me").json()["id"]

    self_ban = client.post(f"/api/admin/users/{admin_id}/ban")
    assert self_ban.status_code == 400
    assert "当前登录" in self_ban.json()["detail"]

    # 同级与更高等级都封不了：admin 动不了另一个 admin，也动不了 superadmin
    peer_email = unique_email("admin2")
    peer_id = register(client, peer_email)
    asyncio.run(set_role_in_db(peer_email, Role.ADMIN))
    peer_ban = client.post(f"/api/admin/users/{peer_id}/ban")
    assert peer_ban.status_code == 400
    assert "同级" in peer_ban.json()["detail"]

    root_email = unique_email("root")
    root_id = register(client, root_email)
    asyncio.run(set_role_in_db(root_email, Role.SUPERADMIN))
    root_ban = client.post(f"/api/admin/users/{root_id}/ban")
    assert root_ban.status_code == 400
    assert "超级管理员" in root_ban.json()["detail"]


def test_cannot_unban_a_peer_or_higher_role(client, monkeypatch):
    """解封与封禁**等级对称**。

    不对称的后果：超管封了一个 admin，随后**任意另一个 admin** 都能把他放回来——
    `ban_user` 想建立的「同级互封不行、收回管理员身份只能走降级」就不成立了。
    """
    # 造一个超管（用来封 admin）与一个普通 admin（用来试越权解封）
    root_email = unique_email("root")
    register(client, root_email)
    asyncio.run(set_role_in_db(root_email, Role.SUPERADMIN))

    actor_email = unique_email("actor")
    register(client, actor_email)
    asyncio.run(set_role_in_db(actor_email, Role.ADMIN))

    # 目标：另一个 admin，已被超管封禁
    target_email = unique_email("target")
    target_id = register(client, target_email)
    asyncio.run(set_role_in_db(target_email, Role.ADMIN))

    with TestClient(create_app()) as root_client:
        login(root_client, root_email)
        banned = root_client.post(f"/api/admin/users/{target_id}/ban")
        assert banned.status_code == 200, banned.text
        assert banned.json()["is_active"] is False

    with TestClient(create_app()) as actor_client:
        login(actor_client, actor_email)

        # admin 解封同级 admin：拒绝（这一步以前会成功）
        peer_unban = actor_client.post(f"/api/admin/users/{target_id}/unban")
        assert peer_unban.status_code == 400
        assert "同级" in peer_unban.json()["detail"]

        # admin 解封超管：同样拒绝
        root_id = root_client.get("/api/auth/me").json()["id"]
        root_unban = actor_client.post(f"/api/admin/users/{root_id}/unban")
        assert root_unban.status_code == 400
        assert "超级管理员" in root_unban.json()["detail"]

    # 超管解封 admin 是合法的（自上而下可以）
    with TestClient(create_app()) as root_client:
        login(root_client, root_email)
        allowed = root_client.post(f"/api/admin/users/{target_id}/unban")
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["is_active"] is True


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


def test_sync_tolerates_duplicate_ids_from_upstream(client, monkeypatch):
    """上游同一个 id 出现两次不能变成 500。

    网关聚合多个上游时这种情况常见。修复前每条重复行都会 `session.add` 一次，提交
    时撞 `uq_provider_model` → `IntegrityError` → 未捕获 → 500，且整次同步（包括
    本来有效的新增）随事务回滚一起丢掉。
    """
    make_admin(client, monkeypatch)
    provider = create_provider(client)

    duplicated = {
        "data": [
            {"id": "vendor/model-a", "display_name": "Model A"},
            {"id": "vendor/model-b", "display_name": "Model B"},
            {"id": "vendor/model-a", "display_name": "Model A (dup)"},
        ]
    }
    monkeypatch.setattr(
        model_service, "get_http_client", lambda: FakeProviderClient(FakeProviderResponse(payload=duplicated))
    )

    response = client.post(f"/api/admin/providers/{provider['id']}/sync")

    assert response.status_code == 200, response.text
    # 重复项既不重复计新增，也不计更新
    assert response.json() == {"fetched": 3, "created": 2, "updated": 0}
    models = client.get(f"/api/admin/providers/{provider['id']}/models").json()
    assert sorted(item["model_id"] for item in models) == ["vendor/model-a", "vendor/model-b"]


def test_sync_repeated_with_duplicates_stays_idempotent(client, monkeypatch):
    """第二次同步（行已存在）时重复 id 走「更新」分支，同样不能 500。"""
    make_admin(client, monkeypatch)
    provider = create_provider(client)
    duplicated = {
        "data": [
            {"id": "vendor/model-a", "display_name": "A1"},
            {"id": "vendor/model-a", "display_name": "A2"},
        ]
    }
    monkeypatch.setattr(
        model_service, "get_http_client", lambda: FakeProviderClient(FakeProviderResponse(payload=duplicated))
    )

    first = client.post(f"/api/admin/providers/{provider['id']}/sync")
    assert first.status_code == 200, first.text
    assert first.json() == {"fetched": 2, "created": 1, "updated": 0}

    second = client.post(f"/api/admin/providers/{provider['id']}/sync")
    assert second.status_code == 200, second.text
    # 只有第一次出现的那条算「更新」，重复的那条跳过
    assert second.json() == {"fetched": 2, "created": 0, "updated": 1}


async def test_sync_translates_integrity_error_into_retryable_message(monkeypatch):
    """并发同步撞唯一约束时要给可读的重试提示，不能裸抛 IntegrityError。"""
    from sqlalchemy.exc import IntegrityError

    from app.db import async_session_maker
    from app.roles import Role as _Role  # noqa: F401 - 仅为保持导入风格一致

    provider = await model_service.create_provider(
        name="并发测试提供商", base_url="http://concurrent.test:4000/v1", api_key="sk-c"
    )
    remote = [{"id": "vendor/x", "display_name": "X"}]
    monkeypatch.setattr(
        model_service,
        "get_http_client",
        lambda: FakeProviderClient(FakeProviderResponse(payload={"data": remote})),
    )

    class ExplodingSession:
        """包一层：commit 时抛 IntegrityError，模拟并发抢占。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def commit(self):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc_info):
            return await self._inner.__aexit__(*exc_info)

    original = async_session_maker

    def patched():
        return ExplodingSession(original())

    monkeypatch.setattr(model_service, "async_session_maker", patched)

    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service.sync_provider_models(provider.id)

    assert "冲突" in str(excinfo.value)
    assert "重试" in str(excinfo.value)
