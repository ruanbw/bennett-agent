import { describe, expect, it } from 'vitest'

import type { SessionRecord } from '../types'
import { pinChange, sortSessions, titleChange, visibleSelection } from './session-list'

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
