import { describe, expect, it } from 'vitest';

import {
  attachmentPreviewTitle,
  attachmentRemoveLabel,
  attachmentTileLabel,
  attachmentTypeLabel,
  attachmentUnsupportedMessage,
  attachmentUploadError,
} from './attachment-labels';

describe('attachmentTypeLabel', () => {
  it('已知类型翻成中文', () => {
    expect(attachmentTypeLabel('image')).toBe('图片');
    expect(attachmentTypeLabel('document')).toBe('文档');
    expect(attachmentTypeLabel('file')).toBe('文件');
  });

  it('未知类型不把内部字符串念给用户', () => {
    expect(attachmentTypeLabel('audio')).toBe('附件');
    expect(attachmentTypeLabel('')).toBe('附件');
  });
});

describe('attachmentTileLabel', () => {
  it('有预览时说明是预览，并带文件名', () => {
    expect(
      attachmentTileLabel({ name: '截图.png', type: 'image', hasPreview: true }),
    ).toBe('预览图片：截图.png');
  });

  it('没有预览时只报类型与文件名', () => {
    expect(attachmentTileLabel({ name: '合同.pdf', type: 'document', hasPreview: false })).toBe(
      '文档：合同.pdf',
    );
  });

  it('上传中 / 失败追加状态，失败优先', () => {
    expect(
      attachmentTileLabel({ name: 'a.png', type: 'image', hasPreview: true, uploading: true }),
    ).toBe('预览图片：a.png，上传中');
    expect(
      attachmentTileLabel({
        name: 'a.png',
        type: 'image',
        hasPreview: true,
        uploading: true,
        error: true,
      }),
    ).toBe('预览图片：a.png，上传失败');
  });
});

describe('附件操作文案', () => {
  it('移除与预览都有中文可读名', () => {
    expect(attachmentRemoveLabel('a.png')).toBe('移除附件 a.png');
    expect(attachmentPreviewTitle('a.png')).toBe('预览附件 a.png');
  });

  it('上传错误回退成中文，不用模板里的 Upload failed', () => {
    expect(attachmentUploadError()).toBe('上传失败');
    expect(attachmentUploadError('   ')).toBe('上传失败');
    expect(attachmentUploadError('磁盘已满')).toBe('磁盘已满');
  });

  it('粘贴/拖入文件的提示带上文件数', () => {
    expect(attachmentUnsupportedMessage(1)).toContain('只接受文本');
    expect(attachmentUnsupportedMessage(3)).toContain('3 个文件');
  });
});
