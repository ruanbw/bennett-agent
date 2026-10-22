import { useCallback, useEffect, useState, type FC } from 'react';
import {
  AlertTriangle,
  BookOpen,
  Loader2,
  Search,
  Trash2,
  Upload,
} from 'lucide-react';

import {
  KNOWLEDGE_READ_TIMEOUT_MS,
  deleteDocument,
  describeDocumentUsage,
  describeKnowledgeStatus,
  fetchDocuments,
  parseCapabilities,
  searchKnowledge,
  uploadDocument,
} from '../lib/knowledge';
import type {
  KnowledgeCapabilities,
  KnowledgeDocumentList,
  KnowledgeSearchHit,
} from '../types';

/**
 * 知识库管理面板。
 *
 * 三个设计决定，都是为了让「它到底有没有在工作」可见：
 *
 * 1. **先看能力再显示表单**：服务端没配 EMBEDDING_MODEL 时上传必失败，先展示原因
 *    比让用户白填一遍好；
 * 2. **显示片段数与上限**：「切成了几块」是分块策略是否合理的唯一直接反馈，
 *    「还剩几篇额度」解释按钮为什么变灰；
 * 3. **给一个手动检索框**：对话里的检索是不可见的（模型可能根本没调那个工具），
 *    用户只能靠这个验证分块与召回效果。
 */
export interface KnowledgePanelProps {
  /** 打开时才拉数据（与「本机数据」区块同一个取舍：不是实时监控）。 */
  open: boolean;
  /** 外部传入的能力快照可省掉一次请求；不传则自己拉。 */
  capabilities?: KnowledgeCapabilities | null;
}

type LoadState = 'idle' | 'loading' | 'ready' | 'failed';

export const KnowledgePanel: FC<KnowledgePanelProps> = ({ open, capabilities = null }) => {
  const [state, setState] = useState<LoadState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [capability, setCapability] = useState<KnowledgeCapabilities | null>(capabilities);
  const [list, setList] = useState<KnowledgeDocumentList | null>(null);
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<KnowledgeSearchHit[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setState('loading');
    setError(null);
    try {
      const next = await fetchDocuments();
      setList(next);
      setState('ready');
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取知识库失败');
      setState('failed');
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void (async () => {
      // 能力一次拉齐：它决定整块 UI 的形态（表单 vs 未启用说明）
      if (!capabilities) {
        const response = await fetch('/api/knowledge/capabilities', {
          credentials: 'include',
          signal: AbortSignal.timeout(KNOWLEDGE_READ_TIMEOUT_MS),
        }).catch(() => null);
        const data = response?.ok ? await response.json().catch(() => null) : null;
        if (!cancelled) setCapability(parseCapabilities(data));
      }
      if (!cancelled) await reload();
    })();
    return () => {
      cancelled = true;
    };
  }, [open, capabilities, reload]);

  const handleUpload = useCallback(async () => {
    if (!title.trim() || !text.trim()) return;
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument({ title: title.trim(), text });
      setTitle('');
      setText('');
      await reload();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '上传失败');
    } finally {
      setUploading(false);
    }
  }, [title, text, reload]);

  const handleDelete = useCallback(
    async (id: string) => {
      try {
        await deleteDocument(id);
        await reload();
      } catch (err) {
        setError(err instanceof Error ? err.message : '删除失败');
      }
    },
    [reload],
  );

  const handleSearch = useCallback(async () => {
    const cleaned = query.trim();
    if (!cleaned) return;
    setSearching(true);
    setSearchError(null);
    try {
      setHits(await searchKnowledge(cleaned));
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : '检索失败');
      setHits(null);
    } finally {
      setSearching(false);
    }
  }, [query]);

  const unavailable = describeKnowledgeStatus(capability);

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
        <BookOpen className="size-3.5" />
        <span>知识库</span>
      </h3>

      {unavailable ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 space-y-1 text-xs">
          <div className="flex items-center gap-1.5 font-medium text-amber-600 dark:text-amber-500">
            <AlertTriangle className="size-3.5" />
            未启用
          </div>
          {/* 说清是部署没配，而不是「你没有权限」或「功能坏了」 */}
          <p className="leading-relaxed text-muted-foreground">{unavailable}</p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
            <div className="flex justify-between items-center">
              <span className="text-muted-foreground">向量模型</span>
              <span className="font-mono text-[11px] text-foreground">
                {capability?.embeddingModel ?? '—'}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-muted-foreground">文档规模</span>
              <span className="text-foreground">{describeDocumentUsage(list)}</span>
            </div>
          </div>

          {state === 'failed' && (
            <div className="flex items-center gap-1.5 text-[11px] text-destructive">
              <AlertTriangle className="size-3" />
              {error}
              <button type="button" onClick={() => void reload()} className="underline">
                重试
              </button>
            </div>
          )}

          {list && list.documents.length > 0 && (
            <ul className="space-y-1">
              {list.documents.map((document) => (
                <li
                  key={document.id}
                  className="flex items-center justify-between gap-2 rounded-md border border-border/70 px-2 py-1.5 text-xs"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-foreground">{document.title}</p>
                    <p className="text-[10.5px] text-muted-foreground">
                      {document.chunkCount} 个片段 · {document.dimensions} 维
                      {document.source ? ` · ${document.source}` : ''}
                    </p>
                  </div>
                  <button
                    type="button"
                    aria-label={`删除 ${document.title}`}
                    onClick={() => void handleDelete(document.id)}
                    className="text-muted-foreground hover:text-destructive focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 rounded p-1"
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </li>
              ))}
            </ul>
          )}

          <details className="rounded-lg border border-border/70 p-2.5 text-xs">
            <summary className="cursor-pointer font-medium text-foreground">上传文档</summary>
            <div className="mt-2 space-y-2">
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="文档标题（引用时会显示）"
                aria-label="文档标题"
                className="w-full rounded-md border border-border/70 bg-background px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              />
              <textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="粘贴文档正文（纯文本或 Markdown）"
                aria-label="文档正文"
                rows={5}
                className="w-full resize-y rounded-md border border-border/70 bg-background px-2 py-1 font-mono text-[11px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              />
              <button
                type="button"
                disabled={uploading || !title.trim() || !text.trim()}
                onClick={() => void handleUpload()}
                className="inline-flex items-center gap-1.5 rounded-md border border-border/70 px-2.5 py-1 text-xs font-medium text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
              >
                {uploading ? <Loader2 className="size-3 animate-spin" /> : <Upload className="size-3" />}
                {uploading ? '入库中…' : '上传并建立索引'}
              </button>
              {uploadError && (
                <p className="text-[11px] leading-relaxed text-destructive">{uploadError}</p>
              )}
              <p className="text-[10.5px] leading-relaxed text-muted-foreground">
                上传后会切分成片段并逐块向量化。标题层级（Markdown 的 #）会作为「章节」保留在引用里。
              </p>
            </div>
          </details>

          <details className="rounded-lg border border-border/70 p-2.5 text-xs">
            <summary className="cursor-pointer font-medium text-foreground">
              手动检索（验证效果）
            </summary>
            <div className="mt-2 space-y-2">
              <div className="flex gap-1.5">
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void handleSearch();
                  }}
                  placeholder="输入一个问题，看会命中哪些片段"
                  aria-label="检索关键词"
                  className="min-w-0 flex-1 rounded-md border border-border/70 bg-background px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
                />
                <button
                  type="button"
                  disabled={searching || !query.trim()}
                  onClick={() => void handleSearch()}
                  className="inline-flex items-center gap-1 rounded-md border border-border/70 px-2 py-1 text-xs hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
                >
                  {searching ? <Loader2 className="size-3 animate-spin" /> : <Search className="size-3" />}
                  检索
                </button>
              </div>
              {searchError && <p className="text-[11px] text-destructive">{searchError}</p>}
              {hits !== null && (
                <div className="space-y-1.5">
                  {hits.length === 0 ? (
                    <p className="text-[10.5px] text-muted-foreground">
                      没有命中任何片段。可能是文档里确实没有相关内容，也可能需要换个说法再试。
                    </p>
                  ) : (
                    hits.map((hit, index) => (
                      <div
                        key={`${hit.documentId}-${index}`}
                        className="rounded border border-border/60 bg-muted/30 p-1.5"
                      >
                        <p className="text-[10.5px] font-medium text-foreground">
                          {hit.documentTitle}
                          {hit.heading ? ` · ${hit.heading}` : ''}
                        </p>
                        <p className="mt-0.5 line-clamp-3 text-[10.5px] leading-relaxed text-muted-foreground">
                          {hit.text}
                        </p>
                        {/* 两路分数分开显示：靠关键词还是靠语义命中，决定了问题出在分块还是嵌入 */}
                        <p className="mt-0.5 font-mono text-[9.5px] text-muted-foreground/70">
                          综合 {hit.score.toFixed(2)} · 语义 {hit.vectorScore.toFixed(2)} · 关键词{' '}
                          {hit.lexicalScore.toFixed(2)}
                        </p>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          </details>
        </div>
      )}
    </div>
  );
};
