import { describe, expect, it } from 'vitest'

import type { SessionRecord } from '../types'
import {
  TITLE_MAX_LENGTH,
  batchDeleteDescription,
  clampTitle,
  deleteSessionDescription,
  isBlankDraft,
  pinChange,
  sortSessions,
  titleChange,
  titleLength,
  visibleSelection,
} from './session-list'

function session(id: string, updatedAt: number, extra: Partial<SessionRecord> = {}): SessionRecord {
  return { id, title: id, createdAt: 0, updatedAt, ...extra }
}

describe('sortSessions', () => {
  it('置顶在前，同组内按最近活动倒序', () => {
    const sessions = [
      session('old', 100),
      session('new', 300),
      session('pinned-old', 50, { isPinned: 1 }),
      session('middle', 200),
    ]
    expect(sortSessions(sessions).map((s) => s.id)).toEqual([
      'pinned-old',
      'new',
      'middle',
      'old',
    ])
  })

  it('不改动传入数组', () => {
    const sessions = [session('a', 100), session('b', 200)]
    sortSessions(sessions)
    expect(sessions.map((s) => s.id)).toEqual(['a', 'b'])
  })
})

describe('titleChange', () => {
  it('标题没变时返回 null：不写库、不 bump', () => {
    expect(titleChange({ title: '旧标题' }, '旧标题', 1_000)).toBeNull()
  })

  it('标题变化时写入新标题与当前时间', () => {
    expect(titleChange({ title: '旧标题' }, '新标题', 1_000)).toEqual({
      title: '新标题',
      updatedAt: 1_000,
    })
  })

  it('只差大小写也算变化', () => {
    expect(titleChange({ title: 'abc' }, 'ABC', 1_000)).toEqual({
      title: 'ABC',
      updatedAt: 1_000,
    })
  })

  it('列表里找不到记录时照写，避免重命名静默丢失', () => {
    expect(titleChange(undefined, '新标题', 1_000)).toEqual({
      title: '新标题',
      updatedAt: 1_000,
    })
  })
})

describe('pinChange', () => {
  it('只写 isPinned，不写 updatedAt', () => {
    expect(pinChange(true)).toEqual({ isPinned: 1 })
    expect('updatedAt' in pinChange(true)).toBe(false)
    expect(pinChange(false)).toEqual({ isPinned: 0 })
  })

  it('置顶/取消置顶不改变组内顺序（按最近活动排，而不是按点按钮的先后）', () => {
    const sessions = [session('a', 300), session('b', 200), session('c', 100)]

    const pinned = sessions.map((s) => (s.id === 'c' ? { ...s, ...pinChange(true) } : s))
    expect(sortSessions(pinned).map((s) => s.id)).toEqual(['c', 'a', 'b'])

    // 取消置顶后回到原来的位置：若 pinChange bump 了 updatedAt，这里会变成 ['c','a','b']
    const unpinned = pinned.map((s) => (s.id === 'c' ? { ...s, ...pinChange(false) } : s))
    expect(sortSessions(unpinned).map((s) => s.id)).toEqual(['a', 'b', 'c'])
  })
})

describe('isBlankDraft', () => {
  it('没有任何消息、没在生成、没置顶就是空占位', () => {
    expect(isBlankDraft(session('s1', 100), 0, false)).toBe(true)
  })

  it('有消息就不是空占位', () => {
    expect(isBlankDraft(session('s1', 100), 1, false)).toBe(false)
  })

  it('正在生成时不算空：流式回答还在内存里，消息条数为 0', () => {
    expect(isBlankDraft(session('s1', 100), 0, true)).toBe(false)
  })

  it('墓碑与已置顶的会话不当作空占位（后者是显式的「留着」）', () => {
    expect(isBlankDraft(session('s1', 100, { deleted: true }), 0, false)).toBe(false)
    expect(isBlankDraft(session('s1', 100, { isPinned: 1 }), 0, false)).toBe(false)
  })

  it('会话行不存在时不当作空占位', () => {
    expect(isBlankDraft(undefined, 0, false)).toBe(false)
  })
})

describe('visibleSelection', () => {
  it('只保留同时可见的选中项', () => {
    const selected = new Set(['a', 'b', 'c'])
    expect([...visibleSelection(selected, ['c', 'd', 'a'])]).toEqual(['c', 'a'])
  })

  it('被搜索隐藏的选中项不进结果，批量删除不会牵连它', () => {
    const selected = new Set(['visible', 'hidden'])
    expect([...visibleSelection(selected, ['visible'])]).toEqual(['visible'])
  })

  it('空选中或空可见集合都返回空集合', () => {
    expect(visibleSelection(new Set(), ['a']).size).toBe(0)
    expect(visibleSelection(new Set(['a']), []).size).toBe(0)
  })
})


describe('标题长度', () => {
  it('按 Unicode 码点计数：emoji 算一个字', () => {
    expect(titleLength('向量数据库')).toBe(5)
    expect(titleLength('😀')).toBe(1)
    expect('😀'.length).toBe(2) // UTF-16 长度是 2，说明这里没有用它
  })

  it('未超限时原样返回', () => {
    expect(clampTitle('短标题')).toEqual({ title: '短标题', truncated: false })
  })

  it('超限时截到 100 个码点并标记 truncated', () => {
    const input = '长'.repeat(TITLE_MAX_LENGTH + 20)
    const result = clampTitle(input)

    expect(result.truncated).toBe(true)
    expect(titleLength(result.title)).toBe(TITLE_MAX_LENGTH)
  })

  it('不会劈开代理对（emoji 序列保持合法）', () => {
    const input = '😀'.repeat(TITLE_MAX_LENGTH + 10)
    const { title, truncated } = clampTitle(input)

    expect(truncated).toBe(true)
    expect(titleLength(title)).toBe(TITLE_MAX_LENGTH)
    // 直接比较内容：劈开代理对会得到长度对不上的乱码
    expect(title).toBe('😀'.repeat(TITLE_MAX_LENGTH))
  })
})

describe('删除确认文案', () => {
  it('单个删除写出消息条数；空标题回退「新对话」', () => {
    expect(deleteSessionDescription('架构讨论', 12, false)).toBe(
      '「架构讨论」及其 12 条消息将被删除，且无法恢复。',
    )
    expect(deleteSessionDescription('   ', 0, false)).toBe('「新对话」将被删除，且无法恢复。')
  })

  it('正在生成的会话明确说明删除会中止回答', () => {
    expect(deleteSessionDescription('长回答', 3, true)).toContain('删除会立即停止回答')
  })

  it('批量删除写出会话数、总消息数与正在生成的个数', () => {
    expect(batchDeleteDescription(2, 30, 1)).toBe(
      '选中的 2 个会话（共 30 条消息）将被删除，且无法恢复。其中 1 个正在生成回答，删除会立即停止。',
    )
    expect(batchDeleteDescription(1, 0, 0)).toBe('选中的 1 个会话将被删除，且无法恢复。')
  })
})
