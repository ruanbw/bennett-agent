import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, Loader2, Pencil, Plus, RefreshCw, Search, Trash2, X } from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import { PageShell, Badge, TableStatusRow, TableWrap, TD_CLASS, TH_CLASS, Toolbar } from './table-parts';
import {
  buildPricePayload,
  deletePrice,
  describeFailure,
  draftFromPrice,
  listPrices,
  setPrice,
  validatePriceDraft,
  type AdminModelPrice,
  type PriceDraft,
  type PriceDraftIssue,
} from '../../lib/admin';
import { cn } from '@/lib/utils';

interface PriceNotice {
  tone: 'success' | 'error';
  text: string;
}

const noticeClass: Record<PriceNotice['tone'], string> = {
  success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  error: 'border-destructive/40 bg-destructive/10 text-destructive',
};

const EMPTY_DRAFT: PriceDraft = {
  inputPerMtok: '',
  outputPerMtok: '',
  cachedInputPerMtok: '',
  currency: 'USD',
};

/**
 * 模型价格管理页。
 *
 * ## 为什么这个页面值得存在
 *
 * 价格是成本展示的**唯一**数据来源。没有录入入口时，前端只能一直显示「未配置价格」，
 * 而有价格的部署又只能改库——那不是一个可用的功能，只是一个能跑的 demo。
 *
 * ## 三条界面约定
 *
 * 1. **空 ≠ 0**。输入框留空表示「未配置」，提交 `null`；填 0 表示免费，提交 `0`。
 *    两种情况在表格里的展示必须不同（「未配置」vs `$0.00`），否则管理员会以为
 *    自己填过价格。
 * 2. **货币是显式字段而不是硬编码 USD**。本地网关可能按人民币计价，硬编码会让所有
 *    金额都错一个汇率。
 * 3. **模型键可以随便填**。价格表的键是 `{provider.slug}/{model_id}`，但这里不做
 *    「必须存在于模型列表」的校验——提前给还没上线的模型备好价格是正常运维需求，
 *    后端也没有外键（见 `app/db.py` 的 `ModelPrice`）。
 */
export function PricePanel() {
  const [prices, setPrices] = useState<AdminModelPrice[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  /** 正在编辑的模型键；`null` = 不在编辑态。 */
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<PriceDraft>(EMPTY_DRAFT);
  const [draftError, setDraftError] = useState<PriceDraftIssue | null>(null);
  const [creating, setCreating] = useState(false);
  const [newModelKey, setNewModelKey] = useState('');

  /** 模型键过滤（客户端过滤：一张表全量展示，键数量级撑不垮浏览器）。 */
  const [keyFilter, setKeyFilter] = useState('');

  const [busy, setBusy] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, PriceNotice>>({});
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const loaded = await listPrices();
        if (!active) return;
        setPrices(loaded);
        setLoadError(null);
      } catch (err) {
        if (!active) return;
        console.error('加载模型价格失败:', err);
        setLoadError(describeFailure(err));
      }
    })();
    return () => {
      active = false;
    };
  }, [reloadToken]);

  const startEdit = useCallback((key: string, existing: AdminModelPrice | null) => {
    setEditingKey(key);
    setDraft(existing ? draftFromPrice(existing) : EMPTY_DRAFT);
    setDraftError(null);
    setNotices((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingKey(null);
    setDraftError(null);
  }, []);

  const save = useCallback(
    async (key: string) => {
      const trimmedKey = key.trim();
      if (!trimmedKey) {
        setDraftError({ field: 'currency', message: '请填写模型键' });
        return;
      }
      const issue = validatePriceDraft(draft);
      if (issue) {
        setDraftError(issue);
        firstFieldRef.current?.focus();
        return;
      }

      setBusy(`save:${trimmedKey}`);
      setDraftError(null);
      try {
        await setPrice(trimmedKey, buildPricePayload(draft));
        setNotices((prev) => ({ ...prev, [trimmedKey]: { tone: 'success', text: '已保存' } }));
        setEditingKey(null);
        setCreating(false);
        setNewModelKey('');
        reload();
      } catch (err) {
        console.error('保存模型价格失败:', err);
        setDraftError({ field: 'currency', message: describeFailure(err) });
      } finally {
        setBusy(null);
      }
    },
    [draft, reload],
  );

  const remove = useCallback(
    async (key: string) => {
      setBusy(`delete:${key}`);
      try {
        await deletePrice(key);
        setNotices((prev) => ({ ...prev, [key]: { tone: 'success', text: '已删除' } }));
        reload();
      } catch (err) {
        console.error('删除模型价格失败:', err);
        setNotices((prev) => ({ ...prev, [key]: { tone: 'error', text: describeFailure(err) } }));
      } finally {
        setBusy(null);
        setPendingDelete(null);
      }
    },
    [reload],
  );

  const filteredPrices = useMemo(() => {
    if (prices === null) return null;
    const needle = keyFilter.trim().toLowerCase();
    if (!needle) return prices;
    return prices.filter((price) => price.model_id.toLowerCase().includes(needle));
  }, [prices, keyFilter]);

  const openCreate = useCallback(() => {
    setNewModelKey('');
    setCreating(true);
    startEdit('', null);
  }, [startEdit]);

  return (
    <PageShell
      title="模型价格"
      description={
        <>
          价格按<strong className="font-medium">每百万 token</strong>填写，用于在对话里估算成本；
          是估算依据而不是账单。留空表示「未配置」（对话显示「未配置价格」），
          填 <code className="font-mono">0</code> 表示免费——两者不同。
        </>
      }
      actions={
        <Button
          size="sm"
          onClick={openCreate}
          disabled={busy !== null || creating}
          className="cursor-pointer gap-1.5"
        >
          <Plus className="size-3.5" aria-hidden="true" />
          新增价格
        </Button>
      }
    >
      {loadError && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span className="min-w-0 flex-1 break-words">{loadError}</span>
          <Button variant="outline" size="xs" onClick={reload} className="cursor-pointer">
            <RefreshCw className="size-3.5" aria-hidden="true" />
            重试
          </Button>
        </div>
      )}

      <Toolbar aria-label="价格筛选">
        <div className="relative w-full max-w-xs min-w-[12rem] flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={keyFilter}
            onChange={(event) => setKeyFilter(event.target.value)}
            placeholder="按模型键过滤"
            aria-label="按模型键过滤价格"
            autoComplete="off"
            className="h-8 pl-8 md:text-xs"
          />
          {keyFilter && (
            <button
              type="button"
              onClick={() => setKeyFilter('')}
              aria-label="清除过滤"
              className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
            >
              <X className="size-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
        {filteredPrices !== null && (
          <span className="text-[11px] text-muted-foreground">
            共 {filteredPrices.length} 条{keyFilter.trim() ? `（总 ${prices?.length ?? 0} 条）` : ''}
          </span>
        )}
      </Toolbar>

      {/* 新建价格：键 + 三个金额一起填，保存后并入表格 */}
      {creating && (
        <div className="rounded-xl border border-border/70 bg-muted/30 px-3 py-3">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <div className="sm:col-span-2 lg:col-span-3">
              <label htmlFor="new-price-key" className="mb-1 block text-xs font-medium text-muted-foreground">
                模型键（<code className="font-mono">{'{提供商 slug}/{模型 id}'}</code>）
              </label>
              <Input
                id="new-price-key"
                value={newModelKey}
                placeholder="my-gateway/openai/gpt-4o"
                autoFocus
                onChange={(event) => setNewModelKey(event.target.value)}
              />
            </div>
            <PriceEditorFields
              idPrefix="new-price"
              draft={draft}
              issue={draftError}
              firstFieldRef={firstFieldRef}
              onChange={(patch) => {
                setDraft((prev) => ({ ...prev, ...patch }));
                setDraftError(null);
              }}
            />
          </div>
          <div className="mt-2.5 flex items-center gap-2">
            <Button size="sm" disabled={busy !== null} onClick={() => void save(newModelKey)}>
              {busy === `save:${newModelKey.trim()}` ? (
                <Loader2 className="mr-1.5 size-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <Check className="mr-1.5 size-3.5" aria-hidden="true" />
              )}
              保存
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => {
                setCreating(false);
                cancelEdit();
              }}
            >
              取消
            </Button>
          </div>
        </div>
      )}

      <TableWrap minWidth="56rem">
        <thead>
          <tr>
            <th className={TH_CLASS}>模型键</th>
            <th className={TH_CLASS}>输入 / 百万 token</th>
            <th className={TH_CLASS}>输出 / 百万 token</th>
            <th className={TH_CLASS}>缓存输入 / 百万 token</th>
            <th className={TH_CLASS}>货币</th>
            <th className={`${TH_CLASS} text-right`}>操作</th>
          </tr>
        </thead>
        <tbody>
          {filteredPrices === null ? (
            !loadError && <TableStatusRow colSpan={6} loading text="正在加载模型价格…" />
          ) : filteredPrices.length === 0 ? (
            keyFilter.trim() ? (
              <TableStatusRow colSpan={6} text={`没有匹配「${keyFilter.trim()}」的价格`} />
            ) : (
              <TableStatusRow
                colSpan={6}
                text="还没有配置任何价格。没有价格时对话里不显示金额（显示「未配置价格」，而不是 $0.00）。"
              />
            )
          ) : (
            filteredPrices.map((price) => {
              const notice = notices[price.model_id];
              const isEditing = editingKey === price.model_id;
              return (
                <PriceRowGroup
                  key={price.model_id}
                  price={price}
                  notice={notice}
                  isEditing={isEditing}
                  busy={busy}
                  draft={draft}
                  draftError={draftError}
                  firstFieldRef={firstFieldRef}
                  onEdit={() => startEdit(price.model_id, price)}
                  onCancelEdit={cancelEdit}
                  onDraftChange={(patch) => {
                    setDraft((prev) => ({ ...prev, ...patch }));
                    setDraftError(null);
                  }}
                  onSave={() => void save(price.model_id)}
                  onRequestDelete={() => setPendingDelete(price.model_id)}
                  onDismissNotice={() =>
                    setNotices((prev) => {
                      const next = { ...prev };
                      delete next[price.model_id];
                      return next;
                    })
                  }
                />
              );
            })
          )}
        </tbody>
      </TableWrap>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title="删除这条价格配置"
        description={
          pendingDelete
            ? `删除后「${pendingDelete}」的对话不再显示金额。历史消息里已记录的金额不受影响。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onConfirm={() => pendingDelete && void remove(pendingDelete)}
      />
    </PageShell>
  );
}

/** 金额的表格单元格：`null` 显示为「未配置」而不是 0。 */
function PriceValue({ value, currency, missingText = '未配置' }: { value: number | null; currency: string; missingText?: string }) {
  const symbols: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€' };
  const symbol = symbols[currency.toUpperCase()] ?? `${currency} `;
  return value === null ? (
    <span className="italic text-muted-foreground/70">{missingText}</span>
  ) : (
    <span className="font-medium">
      {symbol}
      {value}/M
    </span>
  );
}

interface PriceRowGroupProps {
  price: AdminModelPrice;
  notice: PriceNotice | undefined;
  isEditing: boolean;
  busy: string | null;
  draft: PriceDraft;
  draftError: PriceDraftIssue | null;
  firstFieldRef: React.RefObject<HTMLInputElement | null>;
  onEdit: () => void;
  onCancelEdit: () => void;
  onDraftChange: (patch: Partial<PriceDraft>) => void;
  onSave: () => void;
  onRequestDelete: () => void;
  onDismissNotice: () => void;
}

/** 一条价格 = 主行 + 可选的提示行 / 编辑行。 */
function PriceRowGroup({
  price,
  notice,
  isEditing,
  busy,
  draft,
  draftError,
  firstFieldRef,
  onEdit,
  onCancelEdit,
  onDraftChange,
  onSave,
  onRequestDelete,
  onDismissNotice,
}: PriceRowGroupProps) {
  return (
    <>
      <tr className="hover:bg-accent/30">
        <td className={TD_CLASS}>
          <code className="break-all font-mono text-xs">{price.model_id}</code>
        </td>
        <td className={TD_CLASS}>
          <PriceValue value={price.input_per_mtok} currency={price.currency} />
        </td>
        <td className={TD_CLASS}>
          <PriceValue value={price.output_per_mtok} currency={price.currency} />
        </td>
        <td className={TD_CLASS}>
          <PriceValue value={price.cached_input_per_mtok} currency={price.currency} missingText="与输入同价" />
        </td>
        <td className={TD_CLASS}>
          <Badge tone="muted">{price.currency}</Badge>
        </td>
        <td className={`${TD_CLASS} text-right`}>
          <div className="inline-flex items-center gap-1">
            <Button
              variant="ghost"
              size="xs"
              disabled={busy !== null}
              onClick={isEditing ? onCancelEdit : onEdit}
              className="cursor-pointer gap-1"
            >
              {isEditing ? <X className="size-3" aria-hidden="true" /> : <Pencil className="size-3" aria-hidden="true" />}
              {isEditing ? '取消' : '编辑'}
            </Button>
            <Button
              variant="ghost"
              size="xs"
              disabled={busy !== null}
              onClick={onRequestDelete}
              aria-label={`删除 ${price.model_id} 的价格`}
              className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="size-3" aria-hidden="true" />
            </Button>
          </div>
        </td>
      </tr>

      {notice && (
        <tr>
          <td colSpan={6} className="px-3 py-1.5">
            <div
              role={notice.tone === 'error' ? 'alert' : 'status'}
              className={cn('flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-[11px]', noticeClass[notice.tone])}
            >
              <span className="min-w-0 flex-1 break-words">{notice.text}</span>
              <button
                type="button"
                onClick={onDismissNotice}
                aria-label="关闭提示"
                className="shrink-0 cursor-pointer hover:opacity-70"
              >
                <X className="size-3" aria-hidden="true" />
              </button>
            </div>
          </td>
        </tr>
      )}

      {isEditing && (
        <tr>
          <td colSpan={6} className="bg-muted/20 px-3 py-3">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              <PriceEditorFields
                idPrefix={`price-edit-${price.model_id}`}
                draft={draft}
                issue={draftError}
                firstFieldRef={firstFieldRef}
                onChange={onDraftChange}
              />
            </div>
            <div className="mt-2.5 flex items-center gap-2">
              <Button size="sm" disabled={busy !== null} onClick={onSave}>
                {busy === `save:${price.model_id}` ? (
                  <Loader2 className="mr-1.5 size-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Check className="mr-1.5 size-3.5" aria-hidden="true" />
                )}
                保存
              </Button>
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={onCancelEdit}>
                取消
              </Button>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

const DRAFT_FIELDS = [
  { field: 'inputPerMtok', label: '输入 / 百万 token', placeholder: '3' },
  { field: 'outputPerMtok', label: '输出 / 百万 token', placeholder: '15' },
  { field: 'cachedInputPerMtok', label: '缓存输入 / 百万 token', placeholder: '0.3' },
  { field: 'currency', label: '货币', placeholder: 'USD' },
] as const;

/** 草稿字段组：新建与行内编辑共用同一份布局与错误提示接线。 */
function PriceEditorFields({
  idPrefix,
  draft,
  issue,
  firstFieldRef,
  onChange,
}: {
  idPrefix: string;
  draft: PriceDraft;
  issue: PriceDraftIssue | null;
  firstFieldRef: React.RefObject<HTMLInputElement | null>;
  onChange: (patch: Partial<PriceDraft>) => void;
}) {
  const errorId = `${idPrefix}-error`;
  return (
    <>
      {DRAFT_FIELDS.map(({ field, label, placeholder }) => (
        <div key={field}>
          <label htmlFor={`${idPrefix}-${field}`} className="mb-1 block text-xs font-medium text-muted-foreground">
            {label}
          </label>
          <Input
            id={`${idPrefix}-${field}`}
            ref={field === 'inputPerMtok' ? firstFieldRef : undefined}
            value={draft[field]}
            placeholder={placeholder}
            inputMode={field === 'currency' ? undefined : 'decimal'}
            maxLength={field === 'currency' ? 8 : undefined}
            aria-invalid={issue?.field === field ? true : undefined}
            aria-describedby={issue?.field === field ? errorId : undefined}
            onChange={(event) => onChange({ [field]: event.target.value })}
          />
        </div>
      ))}
      {issue && (
        <p id={errorId} role="alert" className="text-xs text-destructive sm:col-span-2 lg:col-span-3">
          {issue.message}
        </p>
      )}
    </>
  );
}
