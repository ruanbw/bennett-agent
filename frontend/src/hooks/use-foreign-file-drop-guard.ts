/**
 * 窗口级文件拖放兜底。
 *
 * 把文件拖到输入框以外的任意位置时，浏览器默认会**导航到那个文件**——整个单页应用
 * 被替换掉，正在生成的回答、未发送的草稿全部丢失，用户还会以为「页面崩了」。
 * 输入框自己处理了框内的拖放；这里补上框外的部分。
 *
 * 判定在 `lib/file-drop.ts`（纯函数、有单测），这里只做事件接线。
 */

import { useEffect } from 'react';

import { dragHasFiles, foreignFileDropMessage, shouldBlockWindowFileDrop } from '../lib/file-drop';
import { notify } from '../lib/toast';

export function useForeignFileDropGuard(): void {
  useEffect(() => {
    // dragover 也要拦：不 preventDefault 的话浏览器连 drop 事件都不会派发，
    // 光标一直显示「可以放置」，松手就直接导航走了。
    const handleDragOver = (event: DragEvent): void => {
      if (dragHasFiles(event.dataTransfer?.types)) event.preventDefault();
    };

    const handleDrop = (event: DragEvent): void => {
      const files = event.dataTransfer?.files.length ?? 0;
      if (
        !shouldBlockWindowFileDrop({
          defaultPrevented: event.defaultPrevented,
          types: event.dataTransfer?.types,
          target: event.target,
        })
      ) {
        return;
      }
      // 拦下导航是必须的；提示只在真的拿到了文件数时给（某些浏览器在 dragover 阶段
      // 读不到 files，此时仍然拦下，只是少一句提示——静默丢掉比导航走好得多）。
      event.preventDefault();
      if (files > 0) notify('info', foreignFileDropMessage(files));
    };

    window.addEventListener('dragover', handleDragOver);
    window.addEventListener('drop', handleDrop);
    return () => {
      window.removeEventListener('dragover', handleDragOver);
      window.removeEventListener('drop', handleDrop);
    };
  }, []);
}
