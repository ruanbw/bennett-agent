import { describe, expect, it } from 'vitest';

import { describeCrash } from './error-boundary-text';

describe('describeCrash', () => {
  it('分块加载失败优先判为「资源加载失败，刷新载入新版本」', () => {
    const report = describeCrash(
      new TypeError('Failed to fetch dynamically imported module: /assets/index-abc.js')
    );

    expect(report.summary).toContain('刷新');
    expect(report.summary).toContain('新版本');
  });

  it('ChunkLoadError 按错误名也能识别', () => {
    const error = new Error('Loading chunk 42 failed.');
    error.name = 'ChunkLoadError';

    expect(describeCrash(error).summary).toContain('刷新');
  });

  it('读到 undefined / null 的属性归为「数据与预期不一致」', () => {
    for (const message of [
      "Cannot read properties of undefined (reading 'content')",
      "Cannot read property 'length' of null",
      'state.parts.map is not a function',
    ]) {
      const report = describeCrash(new TypeError(message));
      expect(report.summary).toContain('数据与预期不一致');
    }
  });

  it('压缩后的 React 错误号不给用户看，但保留在 detail 里', () => {
    const report = describeCrash(new Error('Minified React error #418; visit https://react.dev/errors/418'));

    expect(report.summary).not.toContain('Minified');
    expect(report.detail).toContain('#418');
  });

  it('认不出的异常给通用结论，并把原文留在 detail', () => {
    const report = describeCrash(new Error('something odd happened'));

    expect(report.summary).toContain('未预期的错误');
    expect(report.detail).toContain('something odd happened');
  });

  it('非 Error 输入也能处理（reject 一个字符串、或裸对象）', () => {
    expect(describeCrash('boom').detail).toContain('boom');
    expect(describeCrash({ message: 'object with message' }).detail).toContain('object with message');
    // 完全拿不到信息时 detail 为空串，调用方据此隐藏折叠区
    expect(describeCrash(undefined).detail).toBe('');
    expect(describeCrash(null).detail).toBe('');
  });

  it('detail 里带错误名，便于反馈时定位', () => {
    expect(describeCrash(new RangeError('bad range')).detail).toBe('RangeError: bad range');
  });
});
