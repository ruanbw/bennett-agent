import { Badge } from './table-parts';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '../ui/dialog';
import { formatContextWindow } from '../../lib/admin';
import type { AdminModel } from '../../lib/admin';

export interface ModelDetailDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  model: AdminModel | null;
  providerName: string;
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] items-start gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words text-foreground">{children}</span>
    </div>
  );
}

/**
 * 模型详情弹窗：完整元数据 + 上游原始条目。
 *
 * 列表行只放得下「名称 / 窗口 / 开关」，描述、最大输出、思考等级与原始 JSON 都
 * 收在这里。原始 JSON 是同步那一刻上游 `/v1/models` 条目的**原样**拷贝——解析
 * 白名单之外的字段（owned_by、created、pricing……）只存在于这份拷贝里，用来回答
 * 「上游到底报了什么」。手动添加或从未同步过的模型没有原始数据，明说原因而不是
 * 显示一个空的代码块。
 */
export function ModelDetailDialog({ open, onOpenChange, model, providerName }: ModelDetailDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>模型详情</DialogTitle>
          <DialogDescription>{model ? `${model.model_id} · ${providerName}` : providerName}</DialogDescription>
        </DialogHeader>

        {model && (
          <div className="space-y-3">
            <div className="space-y-1.5">
              <MetaRow label="展示名称">{model.display_name}</MetaRow>
              <MetaRow label="路由键">
                <code className="break-all font-mono text-[11px]">{model.key}</code>
              </MetaRow>
              {model.description && <MetaRow label="描述">{model.description}</MetaRow>}
              <MetaRow label="上下文窗口">{formatContextWindow(model.context_window)}</MetaRow>
              <MetaRow label="最大输出">
                {model.max_output_tokens ? `${model.max_output_tokens} tokens` : '未知'}
              </MetaRow>
              <MetaRow label="思考能力">
                {model.reasoning === null ? (
                  '未知（上游未声明）'
                ) : model.reasoning ? (
                  <span className="inline-flex flex-wrap items-center gap-1">
                    <Badge tone="primary">推理</Badge>
                    {model.supported_reasoning_levels.length > 0 && (
                      <span className="text-muted-foreground">
                        等级：{model.supported_reasoning_levels.join(' / ')}
                      </span>
                    )}
                  </span>
                ) : (
                  '不支持'
                )}
              </MetaRow>
            </div>

            <div className="space-y-1.5">
              <div className="text-xs font-medium text-foreground">上游原始数据</div>
              {model.raw === null ? (
                <p className="text-[11px] text-muted-foreground">
                  暂无：这条模型是手动添加的，或在该功能上线前同步的——重新点一次「同步模型」即可补上。
                </p>
              ) : (
                <pre className="max-h-64 overflow-auto rounded-lg border border-border/70 bg-muted/30 p-2.5 font-mono text-[11px] leading-relaxed">
                  {JSON.stringify(model.raw, null, 2)}
                </pre>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
