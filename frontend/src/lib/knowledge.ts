/**
 * 知识库（RAG）的客户端。
 *
 * 三条边界：
 *
 * 1. **未启用要能解释**：服务端没配 `EMBEDDING_MODEL` 时上传会 409。前端必须先
 *    拿到 `enabled` 再决定是否显示上传表单，否则用户填完标题点提交才看到一句
 *    「知识库未启用」——白填一遍。
 * 2. **失败不静默**：检索是增强能力，但「上传失败」与「检索不到」都必须看得见。
 * 3. **响应要收窄**：服务端增删字段、代理返回错误页时，前端不该因为类型断言拿到
 *    `undefined` 再在渲染里炸掉（与 `capabilities.ts` 同一套做法）。
 */

import type {
  KnowledgeCapabilities,
  KnowledgeDocument,
  KnowledgeDocumentList,
  KnowledgeSearchHit,
} from '../types';
import { apiFetch } from './api';

/** 知识库状态未知时的保守取值：当作不可用，不显示上传表单。 */
export const UNKNOWN_KNOWLEDGE: KnowledgeCapabilities = {
  enabled: false,
  embeddingModel: null,
  reason: null,
};

/**
 * 入库可能很慢：一次上传要分块 + 逐批算向量（几百块就是十几次上游调用）。
 *
 * 这个超时比普通接口宽松得多，但仍必须有——没有超时时用户会看到一个永远转圈的
 * 上传按钮，而失败原因只能靠猜。
 */
export const KNOWLEDGE_UPLOAD_TIMEOUT_MS = 120_000;

/** 列表与检索是普通读请求。 */
export const KNOWLEDGE_READ_TIMEOUT_MS = 15_000;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

async function readErrorDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    const record = asRecord(body);
    const detail = record?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
  } catch {
    // 响应体不是 JSON（反代错误页）：退回状态码文案
  }
  return `${fallback}（HTTP ${response.status}）`;
}

/** 拉知识库可用性；失败时按「不可用」降级而不是抛给调用方。 */
export async function fetchKnowledgeCapabilities(): Promise<KnowledgeCapabilities> {
  try {
    const response = await apiFetch('/api/knowledge/capabilities', {
      timeoutMs: KNOWLEDGE_READ_TIMEOUT_MS,
    });
    if (!response.ok) return UNKNOWN_KNOWLEDGE;
    const data: unknown = await response.json();
    return parseCapabilities(data);
  } catch {
    return UNKNOWN_KNOWLEDGE;
  }
}

export function parseCapabilities(data: unknown): KnowledgeCapabilities {
  const record = asRecord(data);
  if (!record) return UNKNOWN_KNOWLEDGE;
  return {
    enabled: record.enabled === true,
    embeddingModel: typeof record.embedding_model === 'string' ? record.embedding_model : null,
    reason: typeof record.reason === 'string' && record.reason ? record.reason : null,
  };
}

/** 列出当前用户的文档。 */
export async function fetchDocuments(): Promise<KnowledgeDocumentList> {
  const response = await apiFetch('/api/knowledge/documents', {
    timeoutMs: KNOWLEDGE_READ_TIMEOUT_MS,
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response, '读取知识库失败'));
  }
  const data: unknown = await response.json();
  return parseDocumentList(data);
}

export function parseDocumentList(data: unknown): KnowledgeDocumentList {
  const record = asRecord(data);
  const rawDocuments = Array.isArray(record?.documents) ? record.documents : [];
  const documents: KnowledgeDocument[] = [];
  for (const entry of rawDocuments) {
    const parsed = parseDocument(entry);
    if (parsed) documents.push(parsed);
  }
  return {
    documents,
    maxDocuments: typeof record?.max_documents === 'number' ? record.max_documents : 0,
    totalChunks: typeof record?.total_chunks === 'number' ? record.total_chunks : 0,
  };
}

function parseDocument(value: unknown): KnowledgeDocument | null {
  const record = asRecord(value);
  if (!record) return null;
  const { id, title } = record;
  // id 与 title 是渲染键与展示主体：缺一个这条记录就没有意义
  if (typeof id !== 'string' || typeof title !== 'string') return null;
  return {
    id,
    title,
    source: typeof record.source === 'string' ? record.source : null,
    chunkCount: typeof record.chunk_count === 'number' ? record.chunk_count : 0,
    embeddingModel: typeof record.embedding_model === 'string' ? record.embedding_model : '',
    dimensions: typeof record.dimensions === 'number' ? record.dimensions : 0,
    createdAt: typeof record.created_at === 'string' ? record.created_at : '',
  };
}

/** 上传一篇文档；失败时抛出带服务端文案的错误（用户需要知道该拆成几篇）。 */
export async function uploadDocument(input: {
  title: string;
  text: string;
  source?: string | null;
}): Promise<KnowledgeDocument> {
  const response = await apiFetch('/api/knowledge/documents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: input.title,
      text: input.text,
      source: input.source ?? null,
    }),
    timeoutMs: KNOWLEDGE_UPLOAD_TIMEOUT_MS,
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response, '上传失败'));
  }
  const data: unknown = await response.json();
  const parsed = parseDocument(data);
  if (!parsed) throw new Error('上传成功但服务端返回的形状无法识别');
  return parsed;
}

/** 删除一篇文档；404 视为已删除（并发删除同一篇不该报错）。 */
export async function deleteDocument(id: string): Promise<void> {
  const response = await apiFetch(`/api/knowledge/documents/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    timeoutMs: KNOWLEDGE_READ_TIMEOUT_MS,
  });
  if (response.status === 404) return;
  if (!response.ok) {
    throw new Error(await readErrorDetail(response, '删除失败'));
  }
}

/** 手动检索（调试用）：让用户自己验证分块与召回效果。 */
export async function searchKnowledge(query: string, topK = 4): Promise<KnowledgeSearchHit[]> {
  const response = await apiFetch('/api/knowledge/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK }),
    timeoutMs: KNOWLEDGE_READ_TIMEOUT_MS,
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response, '检索失败'));
  }
  const data: unknown = await response.json();
  return parseSearchHits(data);
}

export function parseSearchHits(data: unknown): KnowledgeSearchHit[] {
  const record = asRecord(data);
  const rawHits = Array.isArray(record?.hits) ? record.hits : [];
  const hits: KnowledgeSearchHit[] = [];
  for (const entry of rawHits) {
    const hit = asRecord(entry);
    if (!hit) continue;
    if (typeof hit.document_title !== 'string' || typeof hit.text !== 'string') continue;
    hits.push({
      documentId: typeof hit.document_id === 'string' ? hit.document_id : '',
      documentTitle: hit.document_title,
      heading: typeof hit.heading === 'string' ? hit.heading : '',
      text: hit.text,
      score: typeof hit.score === 'number' ? hit.score : 0,
      vectorScore: typeof hit.vector_score === 'number' ? hit.vector_score : 0,
      lexicalScore: typeof hit.lexical_score === 'number' ? hit.lexical_score : 0,
    });
  }
  return hits;
}

/**
 * 文档规模的一行说明。
 *
 * 「还有几篇额度」必须写出来：上传按钮在额度用完时会变灰，而只显示一个灰按钮，
 * 用户会以为是坏了。
 */
export function describeDocumentUsage(list: KnowledgeDocumentList | null): string {
  if (!list) return '—';
  const count = list.documents.length;
  if (list.maxDocuments <= 0) return `${count} 篇`;
  return `${count} / ${list.maxDocuments} 篇 · ${list.totalChunks} 个片段`;
}

/** 知识库不可用时的解释；可用时返回 null。 */
export function describeKnowledgeStatus(capabilities: KnowledgeCapabilities | null): string | null {
  if (!capabilities || capabilities.enabled) return null;
  return capabilities.reason ?? '服务端未启用知识库（缺少 EMBEDDING_MODEL 配置）';
}
