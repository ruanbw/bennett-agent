"use client";

import { useEffect, useRef, useState } from "react";

export type UseCopyToClipboardOptions = {
  copiedDuration?: number;
};

/**
 * 写剪贴板，返回是否成功。
 *
 * `navigator.clipboard` 只在安全上下文（https / localhost）存在：`http://192.168.x.x`
 * 这类局域网地址下它是 undefined，而旧实现遇到这种情况直接静默返回，用户看到的是
 * 「点了复制，但什么都没有」——既没成功提示也没失败提示。这里依次尝试
 * clipboard API → `document.execCommand('copy')` 临时 textarea 回退，全部失败返回 false。
 */
export async function writeClipboardText(value: string): Promise<boolean> {
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;

  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(value);
      return true;
    } catch {
      // 权限被拒 / 文档失焦 / 非安全上下文：落到 execCommand 回退，而不是直接放弃
    }
  }

  return legacyCopy(value);
}

/** 回退方案：临时 textarea + execCommand。环境里没有 document/execCommand 时返回 false。 */
function legacyCopy(value: string): boolean {
  if (typeof document === "undefined") return false;

  const textarea = document.createElement("textarea");
  textarea.value = value;
  // readonly 避免移动端弹键盘；fixed 定位避免聚焦时页面跳动
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);

  try {
    textarea.select();
    // iOS Safari 只认 setSelectionRange，不认 select()
    textarea.setSelectionRange(0, textarea.value.length);
    return typeof document.execCommand === "function" && document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
}

/**
 * 复制到剪贴板。
 *
 * `isCopied` 保留原有的「已复制」窗口；新增 `error`：复制彻底失败（无 clipboard API
 * 且 execCommand 回退也失败）时为 true，由调用方负责提示用户，避免「静默失败」。
 * 返回值保持 `void`：调用方不需要（也不该）为了一次复制去挂 Promise 链。
 */
export const useCopyToClipboard = ({
  copiedDuration = 3000,
}: UseCopyToClipboardOptions = {}) => {
  const [isCopied, setIsCopied] = useState<boolean>(false);
  const [error, setError] = useState<boolean>(false);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const scopeGenerationRef = useRef(0);

  useEffect(
    () => () => {
      scopeGenerationRef.current += 1;
      // 卸载时只清理定时器：此时 setState 已经没有任何效果（组件不会再渲染）
      if (copiedTimerRef.current !== undefined) {
        clearTimeout(copiedTimerRef.current);
        copiedTimerRef.current = undefined;
      }
    },
    [],
  );

  const copyToClipboard = (value: string) => {
    if (!value) return;

    const scopeGeneration = scopeGenerationRef.current;
    setError(false);

    void writeClipboardText(value).then((succeeded) => {
      // 卸载或已经进入新一轮复制：这次的结果是过期的，丢弃
      if (scopeGeneration !== scopeGenerationRef.current) return;

      if (!succeeded) {
        setError(true);
        return;
      }

      if (copiedTimerRef.current !== undefined) {
        clearTimeout(copiedTimerRef.current);
      }
      setIsCopied(true);
      copiedTimerRef.current = setTimeout(() => {
        copiedTimerRef.current = undefined;
        setIsCopied(false);
      }, copiedDuration);
    });
  };

  return { isCopied, error, copyToClipboard };
};
