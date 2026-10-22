"""模型计价：把 token 用量换算成钱。

## 三个必须写清的边界

**1. 没有价格就不显示金额，不显示 0。**

绝大多数自建网关与本地模型（vLLM、LiteLLM 本地代理）没有公开计价。缺价格时报
「未配置价格」，绝不报 0——0 会被读成「免费」，那是一个明确的错误结论，而且会让人
据此做成本决策。

**2. 这不是账单。**

上游按**自己的**计费周期与规则结算（可能有阶梯价、批量折扣、最低消费、按秒计费）。
这里算的是「按当前登记的价格，这些 token 值多少钱」，用途是**预算感知**：
「这个会话已经花了几毛」「哪个模型贵十倍」。把它当账单对账必然对不上，这个差别要
在界面上也写出来（前端文案里带「估算」）。

**3. 缓存命中的价格不一定等于普通输入价。**

多数厂商给缓存命中一个折扣价（甚至免费），也有厂商同价。所以 `cached_input_per_mtok`
缺失时的语义是「与普通输入同价」，而不是「免费」——这个默认值选错会让成本被低估。

## 精度

金额一律用 `Decimal` 走完全程，只在**展示**时才转成 float/字符串。token 数动辄几十万，
单价是 1e-6 量级，用 float 累加几百轮之后误差会到分位——而这里的数字本来就是「分」
的量级。

## 单位

价格按**每百万 token** 存储与配置（`input_per_mtok`）。这是业界通行的报价单位，也让
配置值落在 `0.15` / `3.00` 这种人类可读的数量级，而不是 `0.00000015`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select

from app.db import ModelPrice, async_session_maker

logger = logging.getLogger(__name__)

#: 百万 token 的除数（价格单位是「每百万 token」）。
TOKENS_PER_MTOK = Decimal(1_000_000)

#: 缺价格时的展示文案。**不返回 0**，理由见模块 docstring。
UNPRICED_LABEL = "未配置价格"

#: 金额展示的小数位。4 位：一分钱是 0.01，四位足够看出「这一轮花了几分钱」的量级，
#: 又不至于满屏 0.0000001。
AMOUNT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class Price:
    """一个模型的价格（每百万 token）。缺省字段为 None = 未知。"""

    model_id: str
    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    #: 缓存命中的输入价。None = 「与普通输入同价」（不是免费）。
    cached_input_per_mtok: Decimal | None = None
    currency: str = "USD"
    #: 这条价格最后一次被改动的时刻。管理界面用它提示「这条价格是不是很久没更新了」
    #: ——上游调价而本地没跟进，是成本估算漂移的最常见来源。
    updated_at: datetime | None = None

    @property
    def is_priced(self) -> bool:
        """能不能算出金额。

        只要输入与输出价有一个缺失，就无法给出完整金额——而给一个「只算了一半」的数
        比不报更糟（用户会以为那就是全部成本）。
        """
        return self.input_per_mtok is not None and self.output_per_mtok is not None

    def effective_cached_input(self) -> Decimal | None:
        """缓存命中的实际单价：没单独配置时退回普通输入价，**不是 0**。

        默认成 0 会把成本算低，而且低得没有上限（缓存命中率高的场景能差出数倍）。
        """
        if self.cached_input_per_mtok is not None:
            return self.cached_input_per_mtok
        return self.input_per_mtok


@dataclass(frozen=True)
class TurnCost:
    """一轮对话的成本明细。"""

    #: 总计（已四舍五入到 `AMOUNT_QUANTUM`）。
    total: Decimal
    currency: str
    #: 输入部分（含缓存命中的那部分，按缓存价算）。
    input_cost: Decimal
    output_cost: Decimal
    #: 缓存命中省下的钱（相对「全部按普通输入价」）。用于告诉用户缓存有没有起作用。
    cache_savings: Decimal

    @property
    def is_free(self) -> bool:
        return self.total == 0


def compute_cost(
    price: Price,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> TurnCost | None:
    """按价格算一轮的成本；价格不全时返回 None（调用方据此显示「未配置价格」）。

    `cache_read_tokens` 是 `input_tokens` 的**子集**（命中缓存的那部分），不是额外开销
    ——所有上游都是这个口径（见 `app/services/usage.py`）。把它从输入里拆出来按缓存价
    计费，而不是加在总数上：加上去会让成本凭空翻倍。
    """
    if not price.is_priced:
        return None

    input_price = price.input_per_mtok or Decimal(0)
    output_price = price.output_per_mtok or Decimal(0)
    cached_price = price.effective_cached_input() or Decimal(0)

    # 缓存命中数不可能超过输入总数；上游偶尔会报出越界值（缓存跨轮复用），
    # 截断而不是相信它——否则输入成本会算出负数。
    cached = max(0, min(cache_read_tokens, input_tokens))
    uncached = input_tokens - cached

    input_cost = (Decimal(uncached) * input_price + Decimal(cached) * cached_price) / TOKENS_PER_MTOK
    output_cost = Decimal(output_tokens) * output_price / TOKENS_PER_MTOK
    # 「省下的钱」按「全部当普通输入算」与「按缓存价算」的差额
    cache_savings = Decimal(cached) * (input_price - cached_price) / TOKENS_PER_MTOK

    return TurnCost(
        total=(input_cost + output_cost).quantize(AMOUNT_QUANTUM),
        currency=price.currency,
        input_cost=input_cost.quantize(AMOUNT_QUANTUM),
        output_cost=output_cost.quantize(AMOUNT_QUANTUM),
        cache_savings=max(Decimal(0), cache_savings).quantize(AMOUNT_QUANTUM),
    )


def parse_price_value(raw: object) -> Decimal | None:
    """把配置值解析成 `Decimal`；空值/非法值返回 None。

    接受字符串与数字：管理后台表单送来的是字符串（`"3.00"`），种子数据里是数字。
    负值一律视为非法（返回 None 而不是 0）：负价格没有任何语义，静默当 0 会让
    「配错了」这件事看不出来。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return None
        raw = cleaned
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("价格值无法解析，按未配置处理: %r", raw)
        return None
    if not value.is_finite() or value < 0:
        logger.warning("价格值非法（必须是非负有限数），按未配置处理: %r", raw)
        return None
    return value


# ---------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------
async def get_price(model_id: str) -> Price | None:
    """读一个模型的价格；没有记录时返回 None。

    **不抛异常**：计价是增强信息，查库失败不该让对话失败。失败时返回 None，前端显示
    「未配置价格」——那比整轮报错好，也比显示 0 好。
    """
    if not model_id:
        return None
    try:
        async with async_session_maker() as session:
            row = await session.scalar(select(ModelPrice).where(ModelPrice.model_id == model_id))
    except Exception:  # noqa: BLE001 - 计价失败不阻断对话
        logger.warning("读取模型价格失败 (model=%s)", model_id, exc_info=True)
        return None
    return _to_price(row) if row is not None else None


def _to_price(row: ModelPrice) -> Price:
    return Price(
        model_id=row.model_id,
        input_per_mtok=row.input_per_mtok,
        output_per_mtok=row.output_per_mtok,
        cached_input_per_mtok=row.cached_input_per_mtok,
        currency=row.currency or "USD",
        updated_at=row.updated_at,
    )


async def list_prices() -> list[Price]:
    """列出全部已登记价格（管理界面用）。"""
    async with async_session_maker() as session:
        rows = (await session.scalars(select(ModelPrice).order_by(ModelPrice.model_id))).all()
    return [_to_price(row) for row in rows]


async def upsert_price(
    model_id: str,
    *,
    input_per_mtok: object = None,
    output_per_mtok: object = None,
    cached_input_per_mtok: object = None,
    currency: str = "USD",
) -> Price:
    """写入或更新一个模型的价格。

    `model_id` 为空直接抛 `ValueError`：它是这张表的业务主键（唯一约束），空值会让
    所有「未命名」价格互相覆盖。
    """
    cleaned_id = (model_id or "").strip()
    if not cleaned_id:
        raise ValueError("model_id 不能为空")

    parsed = {
        "input_per_mtok": parse_price_value(input_per_mtok),
        "output_per_mtok": parse_price_value(output_per_mtok),
        "cached_input_per_mtok": parse_price_value(cached_input_per_mtok),
    }
    clean_currency = (currency or "USD").strip().upper()[:8] or "USD"

    async with async_session_maker() as session:
        row = await session.scalar(select(ModelPrice).where(ModelPrice.model_id == cleaned_id))
        if row is None:
            row = ModelPrice(model_id=cleaned_id)
            session.add(row)
        row.input_per_mtok = parsed["input_per_mtok"]
        row.output_per_mtok = parsed["output_per_mtok"]
        row.cached_input_per_mtok = parsed["cached_input_per_mtok"]
        row.currency = clean_currency
        await session.commit()
        await session.refresh(row)
        return _to_price(row)


async def delete_price(model_id: str) -> bool:
    """删除一个模型的价格；返回是否真的删掉了。"""
    async with async_session_maker() as session:
        result = await session.execute(delete(ModelPrice).where(ModelPrice.model_id == model_id))
        await session.commit()
        return bool(result.rowcount)


__all__ = [
    "AMOUNT_QUANTUM",
    "TOKENS_PER_MTOK",
    "UNPRICED_LABEL",
    "Price",
    "TurnCost",
    "compute_cost",
    "delete_price",
    "get_price",
    "list_prices",
    "parse_price_value",
    "upsert_price",
]
