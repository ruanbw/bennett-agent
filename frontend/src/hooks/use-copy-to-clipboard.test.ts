import { afterEach, describe, expect, it, vi } from 'vitest';

import { writeClipboardText } from './use-copy-to-clipboard';

/**
 * 只测复制这条纯逻辑链路（clipboard API → execCommand 回退 → 失败）。
 *
 * hook 内部的 state 流转要有 React 渲染环境才能测，而前端测试跑在 node 环境、不能引新依赖
 * （没有 jsdom）；因此这里钉住「怎么算成功/失败」这个契约，hook 只做转发。
 */

interface DocumentStubOptions {
  execCommandResult?: boolean;
  execCommandThrows?: boolean;
}

function createDocumentStub({ execCommandResult = true, execCommandThrows = false }: DocumentStubOptions = {}) {
  const appended: unknown[] = [];
  const removed: unknown[] = [];
  const textarea = {
    value: '',
    style: {} as Record<string, string>,
    attributes: {} as Record<string, string>,
    setAttribute(name: string, value: string) {
      this.attributes[name] = value;
    },
    select: vi.fn(),
    setSelectionRange: vi.fn(),
  };

  const documentStub = {
    createElement: vi.fn(() => textarea),
    body: {
      appendChild: (element: unknown) => {
        appended.push(element);
        return element;
      },
      removeChild: (element: unknown) => {
        removed.push(element);
        return element;
      },
    },
    execCommand: vi.fn(() => {
      if (execCommandThrows) throw new Error('execCommand 不可用');
      return execCommandResult;
    }),
  };

  return { documentStub, textarea, appended, removed };
}

const stubClipboard = (writeText: () => Promise<void> | undefined) => {
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn(writeText) } });
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('writeClipboardText', () => {
  it('clipboard API 可用时走 writeText，不碰 execCommand', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { documentStub } = createDocumentStub();
    vi.stubGlobal('document', documentStub);

    await expect(writeClipboardText('hello')).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith('hello');
    expect(documentStub.execCommand).not.toHaveBeenCalled();
  });

  it('没有 clipboard API（http 局域网等非安全上下文）时回退 execCommand', async () => {
    vi.stubGlobal('navigator', {});
    const { documentStub, textarea, appended, removed } = createDocumentStub();
    vi.stubGlobal('document', documentStub);

    await expect(writeClipboardText('hello')).resolves.toBe(true);
    expect(textarea.value).toBe('hello');
    expect(textarea.attributes.readonly).toBe('');
    expect(documentStub.execCommand).toHaveBeenCalledWith('copy');
    // 临时 textarea 必须收回去，不能留在 DOM 里
    expect(appended).toEqual([textarea]);
    expect(removed).toEqual([textarea]);
  });

  it('writeText 被拒时回退 execCommand', async () => {
    stubClipboard(async () => {
      throw new Error('NotAllowedError');
    });
    const { documentStub } = createDocumentStub();
    vi.stubGlobal('document', documentStub);

    await expect(writeClipboardText('hello')).resolves.toBe(true);
    expect(documentStub.execCommand).toHaveBeenCalledWith('copy');
  });

  it('writeText 被拒且 execCommand 返回 false → 失败（调用方据此置 error）', async () => {
    stubClipboard(async () => {
      throw new Error('NotAllowedError');
    });
    const { documentStub, removed, textarea } = createDocumentStub({ execCommandResult: false });
    vi.stubGlobal('document', documentStub);

    await expect(writeClipboardText('hello')).resolves.toBe(false);
    expect(removed).toEqual([textarea]);
  });

  it('execCommand 抛异常时返回失败且不把异常抛出去', async () => {
    vi.stubGlobal('navigator', {});
    const { documentStub, removed, textarea } = createDocumentStub({ execCommandThrows: true });
    vi.stubGlobal('document', documentStub);

    await expect(writeClipboardText('hello')).resolves.toBe(false);
    expect(removed).toEqual([textarea]);
  });

  it('既没有 clipboard 也没有 document（SSR）时返回失败', async () => {
    vi.stubGlobal('navigator', {});
    await expect(writeClipboardText('hello')).resolves.toBe(false);
  });
});
