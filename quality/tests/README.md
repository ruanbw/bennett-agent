# quality 自测与 `quality/` 放在一起，而不是仓库根的 `tests/`

`quality/` 的设计前提是「不依赖任何基础设施也能跑」：它不连 Postgres/Redis，也不依赖
`tests/conftest.py` 注入的环境变量与 fakeredis 替身。测试跟包放在一起，谁改判分器就在
同目录看到它的用例；`pytest quality/tests` 单跑即可，CI 里也用它做独立的一道闸。

根 `tests/` 覆盖的是应用行为（路由、服务、同步引擎），两套测试各自独立，互不拖累。
