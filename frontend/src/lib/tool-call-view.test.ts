import { describe, expect, it } from 'vitest'

import {
  formatToolDuration,
  formatUnknownValue,
  previewToolText,
  resolveToolCallViewStatus,
  TOOL_TEXT_PREVIEW_LIMIT,
} from './tool-call-view'

describe('resolveToolCallViewStatus', () => {
  it('result 缺席时不再算完成，即便运行时把消息标成 complete', () => {
    expect(resolveToolCallViewStatus({ type: 'complete' }, undefined)).toBe('interrupted')
    expect(resolveToolCallViewStatus(undefined, undefined)).toBe('interrupted')
  })

  it('拿到 result 才显示完成，空字符串也算拿到了', () => {
    expect(resolveToolCallViewStatus({ type: 'complete' }, '')).toBe('complete')
    expect(resolveToolCallViewStatus(undefined, '{}')).toBe('complete')
    expect(resolveToolCallViewStatus({ type: 'complete' }, null)).toBe('complete')
  })

  it('运行中的调用保持 running，不会被判成中断', () => {
    expect(resolveToolCallViewStatus({ type: 'running' }, undefined)).toBe('running')
  })

  it('incomplete 与 requires-action 原样透传', () => {
    expect(resolveToolCallViewStatus({ type: 'incomplete', reason: 'cancelled' }, undefined)).toBe(
      'incomplete',
    )
    expect(resolveToolCallViewStatus({ type: 'requires-action', reason: 'tool-calls' }, undefined)).toBe(
      'requires-action',
    )
  })
})

describe('previewToolText', () => {
  it('短文本原样返回，不标记截断', () => {
    expect(previewToolText('短文本')).toEqual({ preview: '短文本', truncated: false })
  })

  it('长度正好等于阈值时不截断', () => {
    const text = 'a'.repeat(TOOL_TEXT_PREVIEW_LIMIT)
    const { preview, truncated } = previewToolText(text)
    expect(truncated).toBe(false)
    expect(preview).toBe(text)
  })

  it('超过阈值时截到阈值并补省略号', () => {
    const text = 'a'.repeat(TOOL_TEXT_PREVIEW_LIMIT + 1)
    const { preview, truncated } = previewToolText(text)
    expect(truncated).toBe(true)
    expect(preview).toBe(`${'a'.repeat(TOOL_TEXT_PREVIEW_LIMIT)}…`)
    expect(preview.length).toBe(TOOL_TEXT_PREVIEW_LIMIT + 1)
  })

  it('阈值可覆盖，便于按调用场景收紧', () => {
    expect(previewToolText('abcdef', 3)).toEqual({ preview: 'abc…', truncated: true })
  })
})

describe('formatUnknownValue', () => {
  it('字符串原样返回（不额外加引号）', () => {
    expect(formatUnknownValue('{"a":1}')).toBe('{"a":1}')
  })

  it('对象按缩进 JSON 输出', () => {
    expect(formatUnknownValue({ a: 1 }, 2)).toBe('{\n  "a": 1\n}')
  })

  it('Error 取字符串形式', () => {
    expect(formatUnknownValue(new Error('boom'))).toBe('Error: boom')
  })

  it('循环引用退化成字符串而不是抛错', () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(formatUnknownValue(circular)).toBe('[object Object]')
  })

  it('连字符串化都失败时给出占位符', () => {
    const hostile = {
      toJSON() {
        throw new Error('no json')
      },
      toString() {
        throw new Error('no string')
      },
    }
    expect(formatUnknownValue(hostile)).toBe('[Unserializable value]')
  })
})

describe('formatToolDuration', () => {
  it('毫秒级显示 <1s', () => {
    expect(formatToolDuration(0)).toBe('<1s')
    expect(formatToolDuration(999)).toBe('<1s')
  })

  it('10 秒以内保留一位小数', () => {
    expect(formatToolDuration(1000)).toBe('1.0s')
    expect(formatToolDuration(1500)).toBe('1.5s')
    expect(formatToolDuration(9900)).toBe('9.9s')
  })

  it('一分钟以内取整秒', () => {
    expect(formatToolDuration(10_000)).toBe('10s')
    expect(formatToolDuration(59_999)).toBe('59s')
  })

  it('超过一分钟显示分秒', () => {
    expect(formatToolDuration(60_000)).toBe('1m 0s')
    expect(formatToolDuration(65_400)).toBe('1m 5s')
  })
})
