"""计价的行为锁。

成本这件事最容易出的错不是算错位数，而是**语义错**——三类都在这里钉住：

1. **缺价格必须是 None，不能是 0。** 显示 0 会被读成「免费」，而且会被人据此做成本
   决策。绝大多数自建网关（vLLM、LiteLLM 本地代理）根本没有计价信息，这条路径是常态
   而不是边界情况。
2. **缓存命中是输入的子集，不是额外开销。** 把它加在输入总数上会让成本凭空翻倍；
   忽略它则会让成本算高（有折扣价的厂商），或者算低（默认成免费）。
3. **`Decimal` 全程不落地成 float。** token 数是几十万量级，单价是 1e-6 量级，
   float 累加几百轮后误差会到分位——而这里的金额本来就是「分」的量级。

数据库用会话级临时库（`tests/conftest.py` 重定向了 `DATABASE_URL`），表在
`create_db_and_tables()` 里建。每个用例自己清空 `model_price`，避免互相污染。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.db import ModelPrice, async_session_maker
from app.services import pricing
from app.services.pricing import (
    Price,
    compute_cost,
    parse_price_value,
)


# ---------------------------------------------------------------------
# compute_cost：纯计算
# ---------------------------------------------------------------------
def _price(**overrides) -> Price:
    base = {
        "model_id": "gw/model-a",
        "input_per_mtok": Decimal("3"),
        "output_per_mtok": Decimal("15"),
    }
    base.update(overrides)
    return Price(**base)


def test_basic_cost_is_input_plus_output():
    cost = compute_cost(_price(), input_tokens=10_000, output_tokens=2_000)

    assert cost is not None
    # 10k × $3/M = $0.03；2k × $15/M = $0.03
    assert cost.input_cost == Decimal("0.0300")
    assert cost.output_cost == Decimal("0.0300")
    assert cost.total == Decimal("0.0600")


def test_unpriced_returns_none_not_zero():
    """缺输入价或输出价时返回 None。返回 0 会被读成「免费」。"""
    assert compute_cost(_price(input_per_mtok=None), input_tokens=100, output_tokens=100) is None
    assert compute_cost(_price(output_per_mtok=None), input_tokens=100, output_tokens=100) is None
    assert compute_cost(Price(model_id="x"), input_tokens=100, output_tokens=100) is None


def test_only_input_price_is_still_unpriced():
    """只配了一个价格不算「能算金额」：只算一半的数比不报更糟。"""
    price = Price(model_id="x", input_per_mtok=Decimal("1"))

    assert price.is_priced is False
    assert compute_cost(price, input_tokens=1000, output_tokens=10) is None


def test_cache_read_is_subset_of_input_not_additive():
    """缓存命中从输入里**拆出来**按缓存价算，不是加在输入总数之上。

    输入 10000（其中 4000 命中），缓存价 $0.3/M：
    6000 × $3/M = $0.018，4000 × $0.3/M = $0.0012，合计输入 $0.0192。
    如果错误地把缓存加在总数之上，输入会变成 14000 token 的钱。
    """
    price = _price(cached_input_per_mtok=Decimal("0.3"))

    cost = compute_cost(price, input_tokens=10_000, output_tokens=2_000, cache_read_tokens=4_000)

    assert cost is not None
    assert cost.input_cost == Decimal("0.0192")
    assert cost.total == Decimal("0.0492")


def test_cache_savings_is_relative_to_normal_input_price():
    """「省了多少钱」= 全部按普通输入价 与 按缓存价 的差额。"""
    price = _price(cached_input_per_mtok=Decimal("0.3"))

    cost = compute_cost(price, input_tokens=10_000, output_tokens=0, cache_read_tokens=4_000)

    assert cost is not None
    # 4000 × ($3 - $0.3)/M = $0.0108
    assert cost.cache_savings == Decimal("0.0108")


def test_missing_cache_price_defaults_to_normal_input_price_not_free():
    """没单独配缓存价 = 「与普通输入同价」，**不是免费**。

    默认成 0 会让成本被低估，而低估没有上限：缓存命中率高的场景能差出数倍。
    """
    price = _price(cached_input_per_mtok=None)
    assert price.effective_cached_input() == Decimal("3")

    cost = compute_cost(price, input_tokens=10_000, output_tokens=0, cache_read_tokens=4_000)

    assert cost is not None
    assert cost.cache_savings == Decimal("0")
    assert cost.input_cost == Decimal("0.03")  # 与没有缓存时一模一样


def test_cache_tokens_beyond_input_are_clamped():
    """上游偶尔会报出超过输入总数的缓存命中（跨轮复用）。不截断会算出负成本。"""
    price = _price(cached_input_per_mtok=Decimal("0.3"))

    cost = compute_cost(price, input_tokens=1_000, output_tokens=0, cache_read_tokens=99_999)

    assert cost is not None
    assert cost.input_cost >= 0
    # 全部按缓存价：1000 × $0.3/M = $0.0003
    assert cost.input_cost == Decimal("0.0003")


def test_negative_cache_tokens_are_clamped():
    price = _price()

    cost = compute_cost(price, input_tokens=1_000, output_tokens=0, cache_read_tokens=-500)

    assert cost is not None
    assert cost.input_cost == Decimal("0.0030")  # 1000 × $3/M


def test_amounts_are_quantized_to_display_precision():
    """金额四舍五入到 4 位。再多的精度在这里没有意义，只会让展示层到处 round。"""
    price = Price(model_id="x", input_per_mtok=Decimal("0.15"), output_per_mtok=Decimal("0.6"))

    cost = compute_cost(price, input_tokens=1, output_tokens=1)

    assert cost is not None
    # 1 token 的钱远小于 0.0001，四舍五入后是 0
    assert cost.total == Decimal("0.0000")
    assert cost.total.as_tuple().exponent == -4


def test_zero_tokens_cost_nothing_but_is_not_free_of_price():
    """0 token 的金额是 0——这与「未配置价格」是两件不同的事。"""
    cost = compute_cost(_price(), input_tokens=0, output_tokens=0)

    assert cost is not None
    assert cost.total == Decimal("0")
    assert cost.is_free is True


def test_decimal_precision_survives_large_token_counts():
    """几十万 token 累加不出浮点误差：用 float 算这个数会差到分位。"""
    price = _price(input_per_mtok=Decimal("0.000001"), output_per_mtok=Decimal("0.000002"))

    cost = compute_cost(price, input_tokens=1_000_000, output_tokens=1_000_000)

    assert cost is not None
    # 1M × $1e-6/M = $1e-6，1M × $2e-6/M = $2e-6 → 合计 $0.000003 → 量化后 0
    assert cost.total == Decimal("0.0000")


def test_currency_is_carried_through():
    cost = compute_cost(_price(currency="CNY"), input_tokens=1_000_000, output_tokens=0)

    assert cost is not None
    assert cost.currency == "CNY"


# ---------------------------------------------------------------------
# parse_price_value
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", Decimal("3")),
        (" 3.50 ", Decimal("3.50")),
        (0.15, Decimal("0.15")),
        (0, Decimal("0")),
        (Decimal("1.234567"), Decimal("1.234567")),
    ],
)
def test_parse_accepts_strings_numbers_and_decimals(raw, expected):
    assert parse_price_value(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "-1", "-0.5", "abc", "NaN", "Infinity"])
def test_parse_rejects_blank_negative_and_garbage(raw):
    """负价格没有语义。静默当 0 会让「配错了」看不出来，所以一律 None。"""
    assert parse_price_value(raw) is None


def test_parse_accepts_zero_but_not_negative():
    """0 是合法的（免费模型），负值不是。这条分界线要明确。"""
    assert parse_price_value("0") == Decimal("0")
    assert parse_price_value("-0.0001") is None


# ---------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _tables():
    """建表并清空价格行。

    `tests/conftest.py` 只重定向 `DATABASE_URL`，**建表**是应用启动才做的；本模块直接
    调服务层，所以自己建一次（幂等）。
    """
    from app.db import create_db_and_tables

    await create_db_and_tables()
    async with async_session_maker() as session:
        await session.execute(delete(ModelPrice))
        await session.commit()
    yield
    async with async_session_maker() as session:
        await session.execute(delete(ModelPrice))
        await session.commit()


async def test_get_price_returns_none_for_unknown_model():
    assert await pricing.get_price("nobody/knows-this") is None


async def test_get_price_returns_none_for_empty_model_id():
    """空键不查库：那会命中一条不该存在的记录。"""
    assert await pricing.get_price("") is None


async def test_upsert_then_get_roundtrips_decimals():
    """写入再读出，Decimal 的精度必须原样保留（不能经 float 中转）。"""
    await pricing.upsert_price(
        "gw/a",
        input_per_mtok="3.000001",
        output_per_mtok="15.5",
        cached_input_per_mtok="0.3",
    )

    price = await pricing.get_price("gw/a")

    assert price is not None
    assert price.input_per_mtok == Decimal("3.000001")
    assert price.output_per_mtok == Decimal("15.5")
    assert price.cached_input_per_mtok == Decimal("0.3")
    assert price.currency == "USD"


async def test_upsert_overwrites_existing_row():
    """同键再写是覆盖，不是新增一行（唯一约束在起作用）。"""
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="2")
    await pricing.upsert_price("gw/a", input_per_mtok="9", output_per_mtok="10")

    price = await pricing.get_price("gw/a")
    assert price is not None
    assert price.input_per_mtok == Decimal("9")
    assert len(await pricing.list_prices()) == 1


async def test_upsert_can_clear_a_price_to_none():
    """把某个价格改回「未知」是合法操作：字段显式传 None 应当清空。"""
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="2")
    await pricing.upsert_price("gw/a", input_per_mtok=None, output_per_mtok="2")

    price = await pricing.get_price("gw/a")
    assert price is not None
    assert price.input_per_mtok is None
    assert price.is_priced is False


async def test_upsert_rejects_blank_model_id():
    with pytest.raises(ValueError):
        await pricing.upsert_price("   ", input_per_mtok="1", output_per_mtok="1")


async def test_upsert_normalizes_currency():
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="1", currency=" cny ")

    price = await pricing.get_price("gw/a")
    assert price is not None
    assert price.currency == "CNY"


async def test_delete_price_reports_whether_it_existed():
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="1")

    assert await pricing.delete_price("gw/a") is True
    assert await pricing.delete_price("gw/a") is False
    assert await pricing.get_price("gw/a") is None


async def test_price_is_independent_of_model_existence():
    """价格表刻意不做外键：未启用的模型可以提前录入价格，删除模型后价格保留。

    这两件事都是真实的运维需求——提前录入（模型还没上线）、保留历史（模型下线了但
    要能解释上个月的成本）。
    """
    price = await pricing.upsert_price("ghost/never-existed", input_per_mtok="1", output_per_mtok="2")

    assert price.model_id == "ghost/never-existed"
    assert await pricing.get_price("ghost/never-existed") is not None


async def test_model_key_with_slashes_is_stored_verbatim():
    """模型键是 `{slug}/{model_id}`，而 model_id 自己带斜杠是常态（`openai/gpt-4o`）。"""
    await pricing.upsert_price("my-gw/openai/gpt-4o", input_per_mtok="2.5", output_per_mtok="10")

    price = await pricing.get_price("my-gw/openai/gpt-4o")
    assert price is not None
    assert price.input_per_mtok == Decimal("2.5")


async def test_list_prices_is_sorted_by_model_id():
    await pricing.upsert_price("gw/b", input_per_mtok="1", output_per_mtok="1")
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="1")

    assert [price.model_id for price in await pricing.list_prices()] == ["gw/a", "gw/b"]


async def test_updated_at_is_populated():
    """管理界面靠它提示「这条价格很久没更新」——上游调价而本地没跟进是成本漂移的主因。"""
    await pricing.upsert_price("gw/a", input_per_mtok="1", output_per_mtok="1")

    price = await pricing.get_price("gw/a")
    assert price is not None
    assert price.updated_at is not None


# ---------------------------------------------------------------------
# 容错：计价失败不能拖死对话
# ---------------------------------------------------------------------
async def test_get_price_swallows_database_errors(monkeypatch):
    """查库失败返回 None 而不是抛异常。

    成本是增强信息，不该成为新的失败点：这条路径上的异常会一路冒到 SSE 流里，让一整轮
    对话因为「查价格失败」而报错。
    """

    class Boom:
        def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("database is on fire")

    # async_session_maker 是模块级名字；换成会抛异常的假实现
    monkeypatch.setattr(pricing, "async_session_maker", Boom())

    assert await pricing.get_price("gw/a") is None
