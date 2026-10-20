/**
 * 版本号必须来自 package.json（构建时注入），不能是第二份手写字面量。
 *
 * 这条测试的价值在于：一旦有人把 `__APP_VERSION__` 的注入删掉、或者组件又写死一个
 * `v0.1.0`，这里会直接失败——而界面上「版本号错了」几乎没人会发现。
 */

import { describe, expect, it } from 'vitest';

import pkg from '../../package.json';
import { APP_VERSION } from './app-info';

describe('APP_VERSION', () => {
  it('等于 package.json 的 version（单一来源）', () => {
    expect(APP_VERSION).toBe(pkg.version);
  });

  it('是 semver，且不是未注入时的兜底值', () => {
    expect(APP_VERSION).toMatch(/^\d+\.\d+\.\d+(?:[-+].+)?$/);
    expect(APP_VERSION).not.toBe('0.0.0-dev');
  });
});
