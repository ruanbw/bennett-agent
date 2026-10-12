import { describe, expect, it } from 'vitest'

import { thinkingSeconds } from './reasoning-time'

describe('thinkingSeconds', () => {
  it('没有耗时或负值显示 0', () => {
    expect(thinkingSeconds(0)).toBe(0)
    expect(thinkingSeconds(-1)).toBe(0)
  })

  it('一毫秒也至少算 1 秒，不会显示 0s', () => {
    expect(thinkingSeconds(1)).toBe(1)
    expect(thinkingSeconds(999)).toBe(1)
  })

  it('向下取整：直播与结算口径一致', () => {
    // 旧的结算路径是 round(1999 / 1000) = 2，直播路径是 floor = 1，
    // 同一段思考会在结束后从 1s 跳成 2s。
    expect(thinkingSeconds(1000)).toBe(1)
    expect(thinkingSeconds(1999)).toBe(1)
    expect(thinkingSeconds(2000)).toBe(2)
  })

  it('长耗时按秒累计', () => {
    expect(thinkingSeconds(59_999)).toBe(59)
    expect(thinkingSeconds(63_000)).toBe(63)
  })
})
