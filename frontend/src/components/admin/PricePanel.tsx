import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Loader2, Pencil, RefreshCw, Trash2, X } from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
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
 * 模型价格管理面板。
 *
 * ## 为什么这个面板值得存在
 *
 * 价格是成本展示的**唯一**数据来源。没有录入入口时，前端只能一直显示「未配置价格」，
 * 而有价格的部署又只能改库——那不是一个可用的功能，只是一个能跑的 demo。
 *
 * ## 三条界面约定
 *
 * 1. **空 ≠ 0**。输入框留空表示「未配置」，提交 `null`；填 0 表示免费，提交 `0`。
 *    两种情况在列表里的展示必须不同（「未配置」vs `$0.00`），否则管理员会以为
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
  const [newModelKey, setNewModelKey] = useState('');

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

  if (loadError) {
    return (
      <div className="space-y-3">
        <p className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {loadError}
        </p>
        <Button variant="outline" size="sm" onClick={reload}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          重试
        </Button>
      </div>
    );
  }

  if (prices === null) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在加载模型价格…
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <p className="rounded-lg border border-border/70 bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
        价格按<strong className="font-medium">每百万 token</strong>填写，用于在对话里估算成本。
        这三个数是<strong className="font-medium">估算依据，不是账单</strong>：上游有阶梯价与折扣，实际以供应商结算为准。
        留空表示「未配置」（界面显示「未配置价格」），填 <code className="font-mono">0</code> 表示免费——两者不同。
      </p>

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[16rem] flex-1">
          <label htmlFor="new-price-key" className="mb-1 block text-xs font-medium text-muted-foreground">
            模型键（<code className="font-mono">{'{提供商 slug}/{模型 id}'}</code>）
          </label>
          <Input
            id="new-price-key"
            value={newModelKey}
            placeholder="my-gateway/openai/gpt-4o"
            onChange={(event) => setNewModelKey(event.target.value)}
          />
        </div>
        <Button
          size="sm"
          disabled={!newModelKey.trim() || busy !== null}
          onClick={() => startEdit(newModelKey.trim(), null)}
        >
          添加价格
        </Button>
      </div>

      {editingKey !== null && !prices.some((price) => price.model_id === editingKey) && (
        <PriceEditor
          modelKey={editingKey}
          draft={draft}
          issue={draftError}
          busy={busy === `save:${editingKey}`}
          firstFieldRef={firstFieldRef}
          onChange={(patch) => {
            setDraft((prev) => ({ ...prev, ...patch }));
            setDraftError(null);
          }}
          onSave={() => void save(editingKey)}
          onCancel={cancelEdit}
        />
      )}

      {prices.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
          还没有配置任何价格。没有价格时对话里不显示金额（显示「未配置价格」，而不是 $0.00）。
        </p>
      ) : (
        <ul className="space-y-2">
          {prices.map((price) => {
            const notice = notices[price.model_id];
            const isEditing = editingKey === price.model_id;
            return (
              <li
                key={price.model_id}
                className="rounded-lg border border-border/70 bg-background px-3 py-2.5"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <code className="break-all font-mono text-xs text-foreground">{price.model_id}</code>
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => (isEditing ? cancelEdit() : startEdit(price.model_id, price))}
                    >
                      {isEditing ? <X className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
                      <span className="ml-1.5">{isEditing ? '取消' : '编辑'}</span>
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => setPendingDelete(price.model_id)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-destructive" />
                    </Button>
                  </div>
                </div>

                <dl className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  <PriceFact label="输入" value={price.input_per_mtok} currency={price.currency} />
                  <PriceFact label="输出" value={price.output_per_mtok} currency={price.currency} />
                  <PriceFact
                    label="缓存输入"
                    value={price.cached_input_per_mtok}
                    currency={price.currency}
                    missingText="与输入同价"
                  />
                </dl>

                {notice && (
                  <p className={cn('mt-2 rounded border px-2 py-1 text-xs', noticeClass[notice.tone])}>
                    {notice.text}
                  </p>
                )}

                {isEditing && (
                  <PriceEditor
                    modelKey={price.model_id}
                    draft={draft}
                    issue={draftError}
                    busy={busy === `save:${price.model_id}`}
                    firstFieldRef={firstFieldRef}
                    onChange={(patch) => {
                      setDraft((prev) => ({ ...prev, ...patch }));
                      setDraftError(null);
                    }}
                    onSave={() => void save(price.model_id)}
                    onCancel={cancelEdit}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}

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
    </div>
  );
}

/** 一行价格；`null` 显示为「未配置」而不是 0。 */
const PriceFact = ({
  label,
  value,
  currency,
  missingText = '未配置',
}: {
  label: string;
  value: number | null;
  currency: string;
  missingText?: string;
}) => {
  const symbols: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€' };
  const symbol = symbols[currency.toUpperCase()] ?? `${currency} `;
  return (
    <div className="flex items-baseline gap-1">
      <dt>{label}</dt>
      <dd className={value === null ? 'italic text-muted-foreground/70' : 'font-medium text-foreground'}>
        {value === null ? missingText : `${symbol}${value}/M`}
      </dd>
    </div>
  );
};

const DRAFT_FIELDS = [
  { field: 'inputPerMtok', label: '输入 / 百万 token', placeholder: '3' },
  { field: 'outputPerMtok', label: '输出 / 百万 token', placeholder: '15' },
  { field: 'cachedInputPerMtok', label: '缓存输入 / 百万 token', placeholder: '0.3' },
] as const;

function PriceEditor({
  modelKey,
  draft,
  issue,
  busy,
  firstFieldRef,
  onChange,
  onSave,
  onCancel,
}: {
  modelKey: string;
  draft: PriceDraft;
  issue: PriceDraftIssue | null;
  busy: boolean;
  firstFieldRef: React.RefObject<HTMLInputElement | null>;
  onChange: (patch: Partial<PriceDraft>) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const errorId = `price-error-${modelKey}`;
  return (
    <div className="mt-2 rounded-lg border border-border/70 bg-muted/30 px-3 py-2.5">
      <div className="grid gap-2 sm:grid-cols-2">
        {DRAFT_FIELDS.map(({ field, label, placeholder }, index) => (
          <div key={field}>
            <label htmlFor={`${errorId}-${field}`} className="mb-1 block text-xs font-medium text-muted-foreground">
              {label}
            </label>
            <Input
              id={`${errorId}-${field}`}
              ref={index === 0 ? firstFieldRef : undefined}
              value={draft[field]}
              placeholder={placeholder}
              inputMode="decimal"
              aria-invalid={issue?.field === field ? true : undefined}
              aria-describedby={issue?.field === field ? errorId : undefined}
              onChange={(event) => onChange({ [field]: event.target.value })}
            />
          </div>
        ))}
        <div>
          <label htmlFor={`${errorId}-currency`} className="mb-1 block text-xs font-medium text-muted-foreground">
            货币
          </label>
          <Input
            id={`${errorId}-currency`}
            value={draft.currency}
            placeholder="USD"
            maxLength={8}
            onChange={(event) => onChange({ currency: event.target.value })}
          />
        </div>
      </div>

      {issue && (
        <p id={errorId} role="alert" className="mt-2 text-xs text-destructive">
          {issue.message}
        </p>
      )}

      <div className="mt-2.5 flex items-center gap-2">
        <Button size="sm" disabled={busy} onClick={onSave}>
          {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Check className="mr-1.5 h-3.5 w-3.5" />}
          保存
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          取消
        </Button>
      </div>
    </div>
  );
}
