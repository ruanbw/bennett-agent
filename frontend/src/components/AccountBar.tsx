/**
 * 侧边栏底部的账号区：当前用户 + 登出。
 *
 * 独立成组件而不是塞进 Sidebar：Sidebar 已经承担「列表 + 选择状态」这一件事，
 * 账号与登出属于另一个变更理由（主题切换当初也是这么拆出去的）。
 *
 * 登出带确认：误点会中断当前正在进行的对话，而登出后要重新输入邮箱和密码才能回来。
 */

import { useState } from 'react';
import { LogOut } from 'lucide-react';

import { ConfirmDialog } from './ui/confirm-dialog';
import { Button } from './ui/button';

export interface AccountBarProps {
  email: string | null;
  onLogout: () => void | Promise<unknown>;
}

export function AccountBar({ email, onLogout }: AccountBarProps) {
  const [confirming, setConfirming] = useState(false);

  return (
    <div className="flex items-center gap-1 border-t border-border px-3 py-2">
      {/* 邮箱可能很长，必须 truncate；title 让鼠标悬停时仍能看到全称 */}
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={email ?? undefined}>
        {email ?? '未登录'}
      </span>

      <Button
        variant="ghost"
        size="icon-sm"
        onClick={() => setConfirming(true)}
        aria-label="登出"
        title="登出"
      >
        <LogOut />
      </Button>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="登出？"
        description="登出后需要重新输入邮箱和密码才能继续对话。"
        confirmLabel="登出"
        destructive
        onConfirm={() => {
          setConfirming(false);
          void onLogout();
        }}
      />
    </div>
  );
}
