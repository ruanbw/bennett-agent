/**
 * 挂进顶栏的「我的电脑」指示器。
 *
 * 这一层只做三件事：拿当前会话 id、读 `sandboxAvailable` 把门、把 `useSandboxStatus`
 * 的结果交给受控的 `SandboxStatus`。把 hook 与展示分开是为了让 `SandboxStatus` 能在
 * node 环境的测试里用 `renderToStaticMarkup` 直接断言（仓库没有 jsdom）。
 *
 * 后端没开沙箱时**整块不渲染**：端点会 404，指示器会永远停在「未知」——对着一个不存在
 * 的功能给用户留一个永远灰着的图标，只会让人以为系统坏了。
 */

import { useCallback, useState } from 'react';

import { useChatSessionId } from '../../context/chat-session';
import { SandboxStatus } from './SandboxStatus';
import { useSandboxStatus } from './use-sandbox-status';

export interface SandboxStatusIndicatorProps {
  /** 服务端是否装配了沙箱（`/api/capabilities` 的 `sandbox`）。false 时整块不渲染。 */
  sandboxAvailable?: boolean;
}

export function SandboxStatusIndicator({ sandboxAvailable = false }: SandboxStatusIndicatorProps) {
  const sessionId = useChatSessionId();
  // 展开状态放在这一层而不是 SandboxStatus 内部：轮询节拍要跟着它变
  // （展开时 20s、收起时 120s）。若让它留在组件里，就得再想办法把变化传出来。
  const [expanded, setExpanded] = useState(false);

  const { status, artifacts, failed, loading, refresh } = useSandboxStatus(
    sessionId,
    sandboxAvailable,
    expanded,
  );

  const handleOpenChange = useCallback((next: boolean) => setExpanded(next), []);

  if (!sandboxAvailable) return null;

  return (
    <SandboxStatus
      status={status}
      artifacts={artifacts}
      failed={failed}
      loading={loading}
      onRefresh={refresh}
      open={expanded}
      onOpenChange={handleOpenChange}
    />
  );
}
