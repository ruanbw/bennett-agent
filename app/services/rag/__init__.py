"""知识库（RAG）：分块、向量化、混合检索。

三段职责分开，因为每一段的失败模式不同：

- `chunking`：纯函数，可以被穷举测试（切点、重叠、碎块合并）；
- `embeddings`：外部边界，失败要降级（检索挂掉不能让对话失败）；
- `store`：数据与检索，隔离与越权在这里收口。

对外的入口是 `store`（入库 / 检索）与 `embeddings.embedding_model`（可用性判定），
模型真正调用的是 `app/tools/knowledge_search.py` 里的工具。
"""
