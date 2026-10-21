/**
 * 附件 UI 的中文文案。
 *
 * 附件组件是从 assistant-ui 模板接过来的，模板里 `Preview {name}`、`Remove {name}`、
 * `Upload failed` 都是英文。这个产品其余界面是中文，混排既突兀，读屏用户在「移除」
 * 按钮上也会听到英文。集中在这里既方便统一，也能脱离 jsdom 做纯函数测试。
 */

export type AttachmentTypeLabel = 'image' | 'document' | 'file';

/** 未知类型不把内部 type 字符串直接念给用户，统一退回「附件」。 */
export function attachmentTypeLabel(type: string): string {
  switch (type) {
    case 'image':
      return '图片';
    case 'document':
      return '文档';
    case 'file':
      return '文件';
    default:
      return '附件';
  }
}

export interface AttachmentTileLabelInput {
  name: string;
  type: string;
  /** 有 blob URL / 可打开预览时才是「预览」，否则只是附件名称。 */
  hasPreview: boolean;
  uploading?: boolean;
  error?: boolean;
}

export function attachmentTileLabel({
  name,
  type,
  hasPreview,
  uploading = false,
  error = false,
}: AttachmentTileLabelInput): string {
  const kind = attachmentTypeLabel(type);
  const base = hasPreview ? `预览${kind}：${name}` : `${kind}：${name}`;
  if (error) return `${base}，上传失败`;
  if (uploading) return `${base}，上传中`;
  return base;
}

export function attachmentRemoveLabel(name: string): string {
  return `移除附件 ${name}`;
}

export function attachmentPreviewTitle(name: string): string {
  return `预览附件 ${name}`;
}

/** 上传错误优先用运行时给的原因，没有就本地化兜底，不要显示 "Upload failed"。 */
export function attachmentUploadError(message?: string | null): string {
  return message?.trim() || '上传失败';
}

/**
 * 用户粘贴/拖入文件时的即时提示。
 *
 * 附件入口已经禁用，但粘贴与拖拽仍可能绕过按钮。此前这些文件被静默忽略：用户以为
 * 图已经带上了。提示必须在他动作发生的这一刻出现，而不是等到发送被禁用。
 */
export function attachmentUnsupportedMessage(count: number): string {
  if (count > 1) return `附件暂不支持：收到 ${count} 个文件，后端目前只接受文本。`;
  return '附件暂不支持：后端目前只接受文本。';
}
