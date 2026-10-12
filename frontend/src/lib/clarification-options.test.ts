import { describe, expect, it } from 'vitest'

import {
  MAX_VISIBLE_CLARIFICATION_OPTIONS,
  sanitizeClarificationOptions,
  shouldShowCustomInput,
  visibleClarificationOptions,
} from './clarification-options'

describe('sanitizeClarificationOptions', () => {
  it('去掉首尾空白', () => {
    expect(sanitizeClarificationOptions(['  按名称 ', '\t按时间\n'])).toEqual(['按名称', '按时间'])
  })

  it('丢弃空串与纯空白，空串药丸不会再出现', () => {
    expect(sanitizeClarificationOptions(['', '   ', '有效'])).toEqual(['有效'])
  })

  it('去重且保留首次出现的顺序', () => {
    expect(sanitizeClarificationOptions(['b', 'a', 'b', ' a '])).toEqual(['b', 'a'])
  })

  it('非字符串项被丢弃', () => {
    const dirty = ['ok', 42, null, undefined, { text: 'x' }] as unknown as string[]
    expect(sanitizeClarificationOptions(dirty)).toEqual(['ok'])
  })

  it('缺失选项返回空数组而不是抛错', () => {
    expect(sanitizeClarificationOptions(undefined)).toEqual([])
    expect(sanitizeClarificationOptions(null)).toEqual([])
  })
})

describe('visibleClarificationOptions', () => {
  const many = Array.from({ length: MAX_VISIBLE_CLARIFICATION_OPTIONS + 3 }, (_, i) => `选项${i}`)

  it('默认最多展示 8 个，其余计数', () => {
    const { visible, hiddenCount } = visibleClarificationOptions(many, false)
    expect(visible).toHaveLength(MAX_VISIBLE_CLARIFICATION_OPTIONS)
    expect(hiddenCount).toBe(3)
    expect(visible[0]).toBe('选项0')
  })

  it('展开后全部可见且不再有折叠计数', () => {
    const { visible, hiddenCount } = visibleClarificationOptions(many, true)
    expect(visible).toHaveLength(many.length)
    expect(hiddenCount).toBe(0)
  })

  it('选项不足上限时不产生「展开更多」', () => {
    const { visible, hiddenCount } = visibleClarificationOptions(['a', 'b'], false)
    expect(visible).toEqual(['a', 'b'])
    expect(hiddenCount).toBe(0)
  })
})

describe('shouldShowCustomInput', () => {
  it('后端允许自定义输入时始终渲染', () => {
    expect(shouldShowCustomInput(3, true)).toBe(true)
    expect(shouldShowCustomInput(0, true)).toBe(true)
  })

  it('后端禁止但清洗后没有有效选项时仍渲染输入框', () => {
    expect(shouldShowCustomInput(0, false)).toBe(true)
  })

  it('后端禁止且有有效选项时才隐藏输入框', () => {
    expect(shouldShowCustomInput(2, false)).toBe(false)
  })
})
