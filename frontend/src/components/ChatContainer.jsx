import React from 'react';
import { Thread } from './assistant-ui/elements/thread.aui';
import { PanelLeft } from 'lucide-react';

export function ChatContainer({
  models,
  modelsError,
  currentModel,
  currentReasoningEffort,
  onSelectModel,
  onSelectReasoningEffort,
  onRefreshModels,
  onToggleSidebar,
}) {
  return (
    <div className="flex-1 flex flex-col h-full bg-background text-foreground overflow-hidden">
      {/* 极简顶栏：只保留侧边栏开关 */}
      <header className="h-12 border-b flex items-center px-3 z-10 shrink-0 bg-background/80 backdrop-blur select-none">
        <button
          onClick={onToggleSidebar}
          className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition cursor-pointer"
          title="切换侧边栏"
          aria-label="切换侧边栏"
        >
          <PanelLeft size={16} />
        </button>
      </header>

      {/* 对话区域：模型选择与思考等级直接集成在输入框底部 */}
      <div className="flex-1 overflow-hidden">
        <Thread
          models={models}
          modelsError={modelsError}
          currentModel={currentModel}
          currentReasoningEffort={currentReasoningEffort}
          onSelectModel={onSelectModel}
          onSelectReasoningEffort={onSelectReasoningEffort}
          onRefreshModels={onRefreshModels}
        />
      </div>
    </div>
  );
}
