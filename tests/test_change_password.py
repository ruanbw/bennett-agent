"""改密端点的规则锁（直接调用路由函数，不起 HTTP 服务）。

本仓库的测试层在提交 2f48724 里刻意移除了端到端/HTTP 层（没有 TestClient 夹具），
而改密又是安全敏感逻辑，不能只靠手测。这里的做法是：直接 await 路由函数、传入伪造的
`user` 与 `user_manager`，锁住四条规则——

1. 当前密码不对 → 400 `CHANGE_PASSWORD_BAD_CREDENTIALS`（**不是 401**：401 会被前端
   的全局处理当成会话过期直接登出，用户就失去了原地重试的机会）；
2. 新旧密码相同 → 400 `CHANGE_PASSWORD_SAME`；
3. 新密码不合规 → 400 `CHANGE_PASSWORD_INVALID: <reason>`，reason 原样透出；
4. 成功 → 204，且新密码是通过 `UserManager.update` 落库的（会话失效由
   `on_after_update` 负责，不在路由里重复实现）。

依赖注入的参数（user / user_manager）可以直接传，这也是不引入 HTTP 层仍然能测的原因。
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi_users.exceptions import InvalidPasswordException

from app.api.auth import change_password
from app.schemas.user import PasswordChangeRequest, UserUpdate


class _FakePasswordHelper:
    def __init__(self, valid: bool) -> None:
        self._valid = valid

    def verify_and_update(self, plain_password: str, hashed_password: str):
        return self._valid, None


class _FakeUserManager:
    def __init__(self, *, valid_current: bool = True, reject_reason: str | None = None) -> None:
        self.password_helper = _FakePasswordHelper(valid_current)
        self.reject_reason = reject_reason
        #: 记录 update 收到的入参；空列表 = 没有落库
        self.updates: list[UserUpdate] = []
        self.request_passed = False

    async def update(self, user_update: UserUpdate, user, request=None):
        self.updates.append(user_update)
        self.request_passed = request is not None
        if self.reject_reason is not None:
            raise InvalidPasswordException(reason=self.reject_reason)
        user.hashed_password = "rehashed"
        return user


class _FakeUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.hashed_password = "$2b$12$fakehash"


def _payload(current: str = "old-password", new: str = "new-password") -> PasswordChangeRequest:
    return PasswordChangeRequest(current_password=current, new_password=new)


async def test_wrong_current_password_is_400_and_nothing_written():
    manager = _FakeUserManager(valid_current=False)

    with pytest.raises(HTTPException) as excinfo:
        await change_password(_payload(), None, _FakeUser(), manager)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "CHANGE_PASSWORD_BAD_CREDENTIALS"
    assert manager.updates == []


async def test_same_password_is_rejected():
    manager = _FakeUserManager()

    with pytest.raises(HTTPException) as excinfo:
        await change_password(_payload("same-password", "same-password"), None, _FakeUser(), manager)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "CHANGE_PASSWORD_SAME"
    assert manager.updates == []


async def test_invalid_new_password_surfaces_framework_reason():
    manager = _FakeUserManager(reject_reason="密码至少需要 8 个字符")

    with pytest.raises(HTTPException) as excinfo:
        await change_password(_payload(new="short"), None, _FakeUser(), manager)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "CHANGE_PASSWORD_INVALID: 密码至少需要 8 个字符"


async def test_success_returns_204_and_updates_through_user_manager():
    manager = _FakeUserManager()
    user = _FakeUser()

    response = await change_password(_payload(), None, user, manager)

    assert response.status_code == 204
    assert len(manager.updates) == 1
    assert manager.updates[0].password == "new-password"
    # 走的是 UserManager.update：会话失效在 on_after_update 里，路由不重复实现
    assert user.hashed_password == "rehashed"
