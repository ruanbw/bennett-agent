/**
 * 侧边栏底部的账号区：当前用户 + 登出。
 *
 * 独立成组件而不是塞进 Sidebar：Sidebar 已经承担「列表 + 选择状态」这一件事，
 * 账号与登出属于另一个变更理由（主题切换当初也是这么拆出去的）。
 *
 * 登出带确认：误点会中断当前正在进行的对话，而登出后要重新输入邮箱和密码才能回来。
 * 确认文案同时说明会清空本机会话记录——登出要清本地库（防跨账号泄漏），不说清就是
 * 悄悄删掉用户的数据。
 *
 * 登出期间按钮显示转圈并禁用：登出请求失败时会重试一次，最长可能要等两个超时周期，
 * 期间没有任何反馈的话用户只会反复点，发出多个登出请求。
 */

import { useCallback, useState } from 'react';
import { Loader2, LogOut } from 'lucide-react';

import { ConfirmDialog } from './ui/confirm-dialog';
import { Button } from './ui/button';

export interface AccountBarProps {
  email: string | null;
  onLogout: () => void | Promise<unknown>;
}

export function AccountBar({ email, onLogout }: AccountBarProps) {
  const [confirming, setConfirming] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await onLogout();
    } catch (err) {
      // 登出本身已经尽力而为（useAuth 内部兜底），这里只保证转圈状态一定会结束
      console.error('登出失败:', err);
    } finally {
      setLoggingOut(false);
    }
  }, [onLogout]);

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
        disabled={loggingOut}
        aria-label={loggingOut ? '正在登出' : '登出'}
        title={loggingOut ? '正在登出' : '登出'}
      >
        {loggingOut ? <Loader2 className="animate-spin" /> : <LogOut />}
      </Button>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="登出？"
        description="登出会清空本机保存的会话记录，之后需要重新输入邮箱和密码才能继续对话。"
        confirmLabel="登出"
        destructive
        onConfirm={() => {
          setConfirming(false);
          void handleLogout();
        }}
      />
    </div>
  );
}
