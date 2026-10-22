import { useState } from 'react';
import { ArrowLeft, ShieldCheck } from 'lucide-react';

import { Button } from '../ui/button';
import { PricePanel } from './PricePanel';
import { ProvidersPanel } from './ProvidersPanel';
import { UsersPanel } from './UsersPanel';
import { cn } from '@/lib/utils';

const TABS = [
  { id: 'providers', label: '模型提供商' },
  // 价格与模型分成两栏而不是塞进模型弹窗里：价格的生命周期独立于模型（可以提前录入、
  // 模型删除后仍保留），而且它是「成本展示」这套功能的唯一数据来源，值得有独立入口。
  { id: 'prices', label: '模型价格' },
  { id: 'users', label: '用户管理' },
] as const;

type AdminTab = (typeof TABS)[number]['id'];

export interface AdminConsoleProps {
  userEmail: string | null;
  /**
   * 当前账号是否 superadmin。
   *
   * 决定「用户管理」里是否出现角色设置按钮：`admin` 能进后台、能封普通用户，
   * 但改角色是最高一档的专属动作（后端同样会拦）。
   */
  canManageRoles: boolean;
  onClose: () => void;
}

/**
 * 管理后台全屏覆盖层。
 *
 * 挂在已登录主界面之上而不是替换它：关闭后对话状态还在原处，不需要重新拉会话。
 * 权限由后端裁决——普通用户若通过其他途径打开，各面板会把 403 渲染成错误条。
 */
export function AdminConsole({ userEmail, canManageRoles, onClose }: AdminConsoleProps) {
  const [activeTab, setActiveTab] = useState<AdminTab>('providers');

  return (
    <div className="fixed inset-0 z-40 overflow-y-auto bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b border-border/70 bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center gap-2 px-4 py-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label="返回对话"
            className="-ml-2 cursor-pointer gap-1.5"
          >
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            返回对话
          </Button>

          <div className="flex min-w-0 flex-1 items-center gap-2">
            <ShieldCheck className="size-4 shrink-0 text-primary" aria-hidden="true" />
            <h1 className="text-sm font-semibold tracking-tight">管理后台</h1>
            <span className="truncate text-[11px] text-muted-foreground" title={userEmail ?? undefined}>
              {userEmail ?? '未登录'}
            </span>
          </div>
        </div>

        <div
          role="tablist"
          aria-label="管理后台分区"
          className="mx-auto flex max-w-4xl gap-1 px-4"
        >
          {TABS.map((tab) => {
            const selected = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                id={`admin-tab-${tab.id}`}
                aria-selected={selected}
                aria-controls={`admin-panel-${tab.id}`}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  'cursor-pointer border-b-2 px-3 py-2 text-xs font-medium transition-colors outline-none focus-visible:ring-1 focus-visible:ring-ring',
                  selected
                    ? 'border-primary text-foreground'
                    : 'border-transparent text-muted-foreground hover:text-foreground',
                )}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-5">
        <div
          role="tabpanel"
          id={`admin-panel-${activeTab}`}
          aria-labelledby={`admin-tab-${activeTab}`}
          tabIndex={-1}
          className="outline-none"
        >
          {activeTab === 'providers' ? (
            <ProvidersPanel />
          ) : activeTab === 'prices' ? (
            <PricePanel />
          ) : (
            <UsersPanel currentUserEmail={userEmail} canManageRoles={canManageRoles} />
          )}
        </div>
      </main>
    </div>
  );
}
