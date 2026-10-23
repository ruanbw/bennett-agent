"""前端托管的行为锁（`app/frontend.py`）。

这些规则都在「看起来能用」和「真的能用」之间：一个能返回页面的 catch-all 很容易写，
但写错的形态不会报错，只会让线上出现一堆难查的症状——

1. `/api/typo` 被回退成 `index.html` → 200 + HTML，前端 JSON 解析失败，日志里没有 404；
2. `/assets/old-hash.js` 被回退成 `index.html` → 浏览器报 `Unexpected token '<'`，
   完全指不到「你部署的是新版本」；
3. `index.html` 被长缓存 → 用户永远拿到旧版本入口；
4. 路径穿越 → 任意文件读取。

所以这里锁的是**四条规则本身**，而不是「挂载有没有生效」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.frontend import (
    ASSETS_CACHE_CONTROL,
    INDEX_CACHE_CONTROL,
    mount_frontend,
    resolve_frontend_dir,
)


# ---------------------------------------------------------------------
# 造一份最小可用的构建产物
# ---------------------------------------------------------------------
@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """一个像 Vite 输出的目录：`index.html` + `assets/<hash>.js` + 一个 favicon。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (root / "assets" / "app-abc123.js").write_text("console.log('hi')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


def _app(dist: Path) -> FastAPI:
    """装一个只含托管逻辑的 app，外加两个「业务」路由用来验证不被吞掉。"""
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    mount_frontend(app, dist)
    return app


@pytest.fixture
def client(dist: Path) -> TestClient:
    return TestClient(_app(dist))


# ---------------------------------------------------------------------
# resolve_frontend_dir
# ---------------------------------------------------------------------
def test_resolve_returns_none_without_index(dist: Path, tmp_path: Path):
    """空目录不算产物：托管它会把所有页面路由到 404，比不托管更难查。"""
    empty = tmp_path / "empty"
    empty.mkdir()

    assert resolve_frontend_dir(str(empty)) is None


def test_resolve_returns_none_for_missing_dir(tmp_path: Path):
    assert resolve_frontend_dir(str(tmp_path / "nope")) is None


def test_resolve_accepts_dir_with_index(dist: Path):
    assert resolve_frontend_dir(str(dist)) == dist


def test_resolve_blank_means_default_location():
    """留空用默认路径；能否真的定位到取决于仓库里有没有构建过前端。

    这里只断言「不会因为空串而抛异常」——本地开发没跑过 `npm run build` 是很正常的，
    那种情况下返回 None 才是对的。
    """
    result = resolve_frontend_dir("")

    assert result is None or (result / "index.html").is_file()


# ---------------------------------------------------------------------
# 规则 1：API 的 404 必须保持 404
# ---------------------------------------------------------------------
def test_unknown_api_path_stays_404_and_json(client: TestClient):
    """`/api/typo` 返 200+HTML 是这类实现最典型的 bug。

    前端 `fetch` 会拿到 200 然后 JSON 解析失败，而服务端日志里一条 404 都没有——
    一个打错的接口看起来「成功了」。
    """
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "<!doctype html" not in response.text.lower()


def test_bare_api_prefix_is_also_404(client: TestClient):
    """/api 本身（没有尾段）也不能被回退成页面。"""
    assert client.get("/api").status_code == 404


def test_registered_api_routes_still_work(client: TestClient):
    """catch-all 不能把已注册的 API 路由吞掉。"""
    response = client.get("/api/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_registered_non_api_routes_still_work(client: TestClient):
    """`/health`、`/docs` 这类非 /api 的已注册路由同样不能被吞掉。"""
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------
# 规则 2：缺失的哈希资源要 404
# ---------------------------------------------------------------------
def test_missing_hashed_asset_is_404_not_html(client: TestClient):
    """换版本后旧页面引用的旧 hash 文件必须给 404。

    回退成 HTML 的话，浏览器会把它当 JS 执行并报 `Unexpected token '<'`——那个报错
    完全指不到真正原因。
    """
    response = client.get("/assets/app-gone999.js")

    assert response.status_code == 404
    assert "console.log" not in response.text
    assert "<!doctype html" not in response.text.lower()


def test_bare_assets_prefix_is_404(client: TestClient):
    assert client.get("/assets").status_code == 404


def test_existing_hashed_asset_is_served(client: TestClient):
    response = client.get("/assets/app-abc123.js")

    assert response.status_code == 200
    assert "console.log" in response.text


# ---------------------------------------------------------------------
# 规则 3：缓存策略
# ---------------------------------------------------------------------
def test_index_is_not_cached(client: TestClient):
    """入口文件是版本切换的唯一入口，缓存住就等于用户永远用旧版本。"""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == INDEX_CACHE_CONTROL


def test_spa_fallback_uses_index_cache_policy(client: TestClient):
    """前端路由回退同样拿 index.html，缓存策略必须与它一致。"""
    response = client.get("/sessions/abc")

    assert response.status_code == 200
    assert response.headers["cache-control"] == INDEX_CACHE_CONTROL


def test_hashed_assets_are_cached_forever(client: TestClient):
    """内容哈希命名 = 内容变了文件名就变，可以放心长缓存。"""
    response = client.get("/assets/app-abc123.js")

    assert response.headers["cache-control"] == ASSETS_CACHE_CONTROL


def test_non_hashed_static_file_is_not_long_cached(client: TestClient):
    """`favicon.svg` 这类文件名**不带**哈希，长缓存会让改名后的替换失效。"""
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["cache-control"] == INDEX_CACHE_CONTROL


# ---------------------------------------------------------------------
# SPA 回退本身
# ---------------------------------------------------------------------
def test_unknown_page_returns_index(client: TestClient):
    response = client.get("/a/deep/client/route")

    assert response.status_code == 200
    assert "<div id=root>" in response.text


def test_root_returns_index(client: TestClient):
    response = client.get("/")

    assert response.status_code == 200
    assert "<div id=root>" in response.text


def test_served_file_matches_disk(dist: Path, client: TestClient):
    response = client.get("/favicon.svg")

    assert response.text == (dist / "favicon.svg").read_text()


# ---------------------------------------------------------------------
# 规则 4：目录穿越
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/../app/config/settings.py",
        "/..%2fapp%2fconfig%2fsettings.py",
        "/assets/../../app/db.py",
        "/....//app/config/settings.py",
    ],
)
def test_traversal_never_leaks_source(dist: Path, client: TestClient, path: str):
    """任何形态的穿越都不能读到产物目录之外的文件。

    允许 200（部分形态会被 Starlette 规范化成一个不存在的普通路径，进而走 SPA 回退），
    但**响应体里不能有源码**——那才是这条规则要防的事。
    """
    response = client.get(path)

    assert "class Settings" not in response.text
    assert "DeclarativeBase" not in response.text
    assert "AUTH_SECRET" not in response.text


def test_traversal_outside_root_is_rejected(dist: Path, client: TestClient):
    """直接构造穿越路径（绕过 URL 规范化）时必须被显式拒绝。"""
    # `..%2f` 在 Starlette 里会被解码成 `../`，但上面的用例已被规范化拦截，
    # 这里用一个更深、能真正到达校验点的形态。
    response = client.get("/assets/%2e%2e/%2e%2e/app/db.py")

    assert response.status_code == 404


# ---------------------------------------------------------------------
# 启动横幅的提示（`main.frontend_hint`）
#
# 三种状态对应三种完全不同的操作，说错一句就把人引到不存在的流程上：
# 托管了还用「另开 Vite」会让人以为服务没起来；没托管却不说，访问根路径 404
# 会以为部署失败。
# ---------------------------------------------------------------------
def test_hint_says_mounted_when_dist_exists(dist: Path, monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "serve_frontend", True)
    monkeypatch.setattr(main.settings, "frontend_dir", str(dist))

    assert "本进程托管" in main.frontend_hint()


def test_hint_tells_you_to_start_vite_when_no_dist(tmp_path: Path, monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "serve_frontend", True)
    monkeypatch.setattr(main.settings, "frontend_dir", str(tmp_path / "missing"))

    hint = main.frontend_hint()
    assert "npm run dev" in hint
    assert "本进程托管" not in hint


def test_hint_respects_explicit_disable(dist: Path, monkeypatch):
    """产物在、但显式关了托管：这时应当说「你自己提供」，而不是让人去起 Vite。"""
    import main

    monkeypatch.setattr(main.settings, "serve_frontend", False)
    monkeypatch.setattr(main.settings, "frontend_dir", str(dist))

    hint = main.frontend_hint()
    assert "SERVE_FRONTEND=false" in hint
    assert "npm run dev" not in hint
