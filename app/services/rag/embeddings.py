"""Embedding 客户端：把文本变成向量，走**已配置的提供商**而不是新加一套配置。

## 为什么复用模型提供商

项目已经有一套「提供商 + API Key + base_url」的配置与出站守卫
（`app/services/model_service/outbound_guard.py`）。Embedding 只是同一网关的另一个
端点（OpenAI 兼容的 `POST {base}/embeddings`），再加一套 `EMBEDDING_API_BASE`
就等于把「谁来管密钥」「谁来做出站校验」这两件事又答一遍，且答案会漂移。

所以这里只多一个配置：**用哪个模型做 embedding**（`EMBEDDING_MODEL`）。地址与密钥
从现有的默认提供商来。管理员换网关时，对话与检索一起跟着换。

## 维度不写死在配置里

不同 embedding 模型维度差别很大（384 / 768 / 1024 / 3072）。维度写进
`EMBEDDING_DIMENSIONS` 会让「换了模型忘了改维度」变成静默的错误检索——向量还在，
只是比较出来的相似度没有意义。这里**从第一次真实响应里读**维度并入库，切换模型时
维度变化会被显式检测到（`EmbeddingDimensionMismatch`），要求重建索引。

## 失败语义

检索是**增强**能力：embedding 服务不可用时，对话必须照常进行（只是没有知识库
查询）。所以调用方拿到的是 `None` / 空结果，而不是异常穿透到 SSE。
但原因要记日志，否则「知识库忽然检索不到东西」在现场无从解释。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from app.config import settings
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)

#: 单次 embedding 请求的超时（秒）。入库是批量后台动作，查询是交互路径上的一步，
#: 两者都不该拖过这个数——知识库检索失败时，用户应该拿到一个「没有引用」的普通回答，
#: 而不是等 30 秒。
EMBEDDING_TIMEOUT_SECONDS = 15.0

#: 单次请求最多送多少条文本。多数 OpenAI 兼容网关的上限是 2048；
#: 取 64 是为了让单次请求的响应体不至于太大，同时减少往返次数。
EMBEDDING_BATCH_SIZE = 64

#: 单条文本的字符上限。embedding 模型的上下文通常 512~8192 token；超长文本会被
#: 上游截断（静默丢掉尾部）或直接 400。入库前分块已经限长，这里是第二道保险。
MAX_EMBEDDING_INPUT_CHARS = 6_000


class EmbeddingError(RuntimeError):
    """embedding 调用失败（配置缺失、上游报错、响应形状不对）。

    单独一个类型是为了让调用方能区分「没有配置 embedding」与「配置了但调用失败」：
    前者是正常部署（不启用知识库），后者是要报警的故障。
    """


class EmbeddingDimensionMismatch(EmbeddingError):
    """返回的向量维度与索引里记录的不一致。

    典型成因是换了 `EMBEDDING_MODEL` 却没有重建索引。此时**不能**继续用：新旧
    向量不在同一个空间里，余弦相似度算出来是噪声，而检索仍会「成功返回」若干条
    结果——比直接报错危险得多。
    """


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimensions: int


def _build_embeddings_url(base_url: str) -> str:
    """拼 embedding 端点地址。

    与 `gateway._build_models_url` 同一条规则：兼容 `http://host:4000` 与
    `http://host:4000/v1` 两种写法。重复拼 `/v1` 会让网关返回 404，而 404 很容易
    被误读成「embedding 服务不可用」。
    """
    base = base_url.strip().rstrip("/")
    return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"


#: 「占位模型名」的形状。`is_usable_api_key` 的占位规则是给**密钥**写的
#: （`your-api-key`、`sk-xxx`），套在模型名上会漏掉 `your-embedding-model-here`
#: 这类写法——它会被当成已配置，于是入库任务跑起来再逐条 401。
_PLACEHOLDER_MODEL = re.compile(
    r"(?:^|[-_ ])(?:your|my)[-_ ].*(?:model|embedding)"
    r"|placeholder|change[-_ ]?me|replace[-_ ]?me|example|xxx+$",
    re.IGNORECASE,
)


def embedding_model() -> str | None:
    """当前配置的 embedding 模型；未配置时返回 None（= 不启用知识库）。

    空白与**占位值**都视为未配置。占位值单独判而不是复用 `is_usable_api_key`：
    那条规则认的是密钥形状（`your-api-key`），对 `EMBEDDING_MODEL=your-embedding-model`
    不起作用，而后者恰恰是最常见的抄示例写法——一旦被当成已配置，入库任务会跑起来
    再逐条 401，用户看到的是「文档传了但检索永远没结果」。
    """
    model = settings.embedding_model
    if model is None:
        return None
    cleaned = model.strip()
    if not cleaned:
        return None
    if _PLACEHOLDER_MODEL.search(cleaned):
        return None
    return cleaned


def _normalize_inputs(texts: list[str]) -> list[str]:
    """空文本替换为单个空格，超长截断。

    上游对空串的处理不一致（有的返回零向量，有的 400）。零向量会污染索引：它与任何
    查询的余弦相似度都是 0，永远排不进结果，但会占一个名额和一份存储。
    """
    normalized: list[str] = []
    for text in texts:
        cleaned = text.strip()
        if not cleaned:
            # 用空格而不是跳过：跳过会让返回的向量数与输入数对不上，
            # 调用方按 zip 配对时就会错位。
            normalized.append(" ")
            continue
        normalized.append(cleaned[:MAX_EMBEDDING_INPUT_CHARS])
    return normalized


async def embed_texts(
    texts: list[str],
    *,
    target: object | None = None,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingResult:
    """把一批文本转成向量。

    `target` 是 `ModelTarget`（可空）：给了就用它的 base_url / api_key，否则从默认
    提供商解析。传对象而不是两个字符串，是为了让调用方复用**已经查过库的那一份**
    配置——否则一次检索要读两遍库（与 `resolve_context_budget` 的
    `model_limits` 参数同一个理由）。

    失败一律抛 `EmbeddingError`（或其子类），由调用方决定降级。
    """
    if not texts:
        return EmbeddingResult(vectors=[], model=embedding_model() or "", dimensions=0)

    model = embedding_model()
    if model is None:
        raise EmbeddingError("未配置 EMBEDDING_MODEL，知识库检索不可用")

    base_url, api_key = await _resolve_endpoint(target)
    url = _build_embeddings_url(base_url)

    http = client or get_http_client()
    vectors: list[list[float]] = []
    dimensions = 0

    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = _normalize_inputs(texts[start : start + EMBEDDING_BATCH_SIZE])
        payload = {"model": model, "input": batch}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        try:
            response = await http.post(
                url,
                json=payload,
                headers=headers,
                timeout=EMBEDDING_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding 请求失败：{exc}") from exc

        if response.status_code >= 400:
            # 只带状态码与一小段正文：错误体里可能含上游地址与请求回显，
            # 而调用方拿它写日志。
            detail = response.text[:200].replace("\n", " ")
            raise EmbeddingError(f"embedding 上游返回 {response.status_code}：{detail}")

        try:
            body = response.json()
            batch_vectors = _parse_embeddings_response(body, expected=len(batch))
        except (ValueError, KeyError) as exc:
            raise EmbeddingError(f"embedding 响应形状不符合预期：{exc}") from exc

        for vector in batch_vectors:
            if dimensions == 0:
                dimensions = len(vector)
            elif len(vector) != dimensions:
                # 同一批里维度不一致 = 上游行为异常，入库会是脏数据
                raise EmbeddingError(f"同一批 embedding 维度不一致：{dimensions} 与 {len(vector)}")
            vectors.append(vector)

    return EmbeddingResult(vectors=vectors, model=model, dimensions=dimensions)


def _parse_embeddings_response(body: object, *, expected: int) -> list[list[float]]:
    """从 OpenAI 兼容响应里取出向量，并**按 `index` 排序**。

    为什么必须排序：规范保证 `data` 里每项带 `index`，但不保证数组已按它排好。
    直接按到达顺序 zip 会把 A 文本的向量配给 B 块——这种错位不会报任何错，只会让
    检索结果莫名其妙地不相关。
    """
    if not isinstance(body, dict):
        raise ValueError("响应不是对象")
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("响应缺少 data 数组")
    if len(data) != expected:
        raise ValueError(f"期望 {expected} 条向量，实际 {len(data)} 条")

    indexed: list[tuple[int, list[float]]] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("data 里的条目不是对象")
        raw_index = item.get("index", position)
        if not isinstance(raw_index, int):
            raise ValueError("index 不是整数")
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("embedding 不是非空数组")
        vector: list[float] = []
        for value in embedding:
            if not isinstance(value, int | float):
                raise ValueError("embedding 里含非数值")
            vector.append(float(value))
        indexed.append((raw_index, vector))

    indexed.sort(key=lambda pair: pair[0])
    return [vector for _, vector in indexed]


async def _resolve_endpoint(target: object | None) -> tuple[str, str | None]:
    """取 (base_url, api_key)：优先用传入的 target，否则查默认提供商。"""
    base_url = getattr(target, "base_url", None)
    api_key = getattr(target, "api_key", None)
    if isinstance(base_url, str) and base_url.strip():
        return base_url, api_key if isinstance(api_key, str) else None

    from app.services.model_service import get_default_model_key, resolve_model_target

    key = await get_default_model_key()
    if key is None:
        raise EmbeddingError("没有可用的默认模型，无法定位 embedding 网关")
    resolved = await resolve_model_target(key)
    if resolved is None:
        raise EmbeddingError(f"默认模型 {key} 没有可用的提供商配置，无法定位 embedding 网关")
    return resolved.base_url, resolved.api_key


__all__ = [
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_TIMEOUT_SECONDS",
    "MAX_EMBEDDING_INPUT_CHARS",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "EmbeddingResult",
    "embed_texts",
    "embedding_model",
]
