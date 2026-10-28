import { describe, expect, it } from 'vitest';

import { formatNotice, messageTextForCopy, stripNotices } from './message-copy';

describe('messageTextForCopy', () => {
  it('只拼接文本分片，跳过思考与工具调用', () => {
    const text = messageTextForCopy([
      { type: 'reasoning', text: '内部草稿' },
      { type: 'text', text: '第一段' },
      { type: 'tool-call', text: '不该出现' },
      { type: 'text', text: '第二段' },
    ]);

    expect(text).toBe('第一段\n\n第二段');
    expect(text).not.toContain('内部草稿');
    expect(text).not.toContain('不该出现');
  });

  it('空分片、缺字段与空数组都退化为空串', () => {
    expect(messageTextForCopy([])).toBe('');
    expect(messageTextForCopy(null)).toBe('');
    expect(messageTextForCopy(undefined)).toBe('');
    expect(messageTextForCopy([{ type: 'text', text: '' }])).toBe('');
    expect(messageTextForCopy([{ type: 'text' }])).toBe('');
  });

  it('去掉首尾空白：只靠空白的分片不该让复制按钮可用', () => {
    expect(messageTextForCopy([{ type: 'text', text: '  \n ' }])).toBe('');
    expect(messageTextForCopy([{ type: 'text', text: '  有内容  ' }])).toBe('有内容');
  });
});

/**
 * 系统提示必须与「复制回答」隔开。
 *
 * 失败/断流时 `chat-store` 会把提示拼在正文尾部（见 `appendNotice` / `setRunError`），
 * 而复制只认文本分片，于是提示会跟着回答一起进剪贴板——用户把一段内部告警贴到别处，
 * 却以为那是模型说的话。
 */
describe('stripNotices', () => {
  it('剥掉结尾带 detail 的提示块', () => {
    expect(stripNotices('正常回答\n\n> ⚠️ **请求失败**: 网络已断开')).toBe('正常回答')
  })

  it('剥掉结尾不带 detail 的提示块', () => {
    expect(stripNotices('正常回答\n\n> ⚠️ **连接已中断**')).toBe('正常回答')
  })

  it('连续多段一起剥干净（断 3 次就有 3 段）', () => {
    const text =
      '半截回答\n\n> ⚠️ **连接已中断**: 回复可能不完整\n\n> ⚠️ **请求失败**: 网络已断开\n\n> ⚠️ **请求失败**: 网络已断开'

    expect(stripNotices(text)).toBe('半截回答')
  })

  it('正文中间的同形引用不动（只剥结尾）', () => {
    const text = '开头。\n\n> ⚠️ **注意**：这样做有风险。\n\n结尾。'

    expect(stripNotices(text)).toBe(text)
  })

  it('全角冒号的引用不是系统提示（模型自己的写法），原样保留', () => {
    const text = '回答\n\n> ⚠️ **注意**：这里有风险'

    expect(stripNotices(text)).toBe(text)
  })

  it('没有提示时原样返回（不做任何无谓改写）', () => {
    expect(stripNotices('就是一段普通回答')).toBe('就是一段普通回答')
    expect(stripNotices('')).toBe('')
  })

  it('与写入端共用同一个形状：formatNotice 产出的每一段都能被剥掉', () => {
    const withDetail = `回答${formatNotice('请求失败', '网络已断开')}`
    const withoutDetail = `回答${formatNotice('连接已中断')}`

    expect(stripNotices(withDetail)).toBe('回答')
    expect(stripNotices(withoutDetail)).toBe('回答')
  })
})

describe('messageTextForCopy 剥掉系统提示', () => {
  it('失败轮复制出来的只有回答正文', () => {
    const text = messageTextForCopy([
      { type: 'text', text: '这是一段真实回答。\n\n> ⚠️ **请求失败**: 网络已断开' },
    ])

    expect(text).toBe('这是一段真实回答。')
    expect(text).not.toContain('⚠️')
    expect(text).not.toContain('请求失败')
  })

  it('只有提示、没有正文时复制结果是空串（按钮随之禁用）', () => {
    expect(messageTextForCopy([{ type: 'text', text: '\n\n> ⚠️ **请求失败**: 网络已断开' }])).toBe(
      ''
    )
  })

  it('提示落在最后一段分片时也剥得掉（先拼接再剥离）', () => {
    const text = messageTextForCopy([
      { type: 'text', text: '第一段' },
      { type: 'text', text: '第二段\n\n> ⚠️ **连接已中断**: 回复可能不完整' },
    ])

    expect(text).toBe('第一段\n\n第二段')
  })
})

describe('提示的形状由写入端保证（一段提示 = 一行引用块）', () => {
  it('detail 里的换行与多余空白被折成单行，剥离端因此总能剥掉', () => {
    const notice = formatNotice('请求失败', '连接被重置\n第二行说明\n\n第三行')

    expect(notice).toBe('\n\n> ⚠️ **请求失败**: 连接被重置 第二行说明 第三行')
    expect(stripNotices(`回答内容${notice}`)).toBe('回答内容')
  })

  it('detail 只有空白时退化为不带 detail 的形状（不留一个空的 ": "）', () => {
    expect(formatNotice('连接已中断', '   ')).toBe('\n\n> ⚠️ **连接已中断**')
    expect(formatNotice('连接已中断', '')).toBe('\n\n> ⚠️ **连接已中断**')
  })

  it('正文里出现换行后再接同形引用、且后面还有正文时，一个字都不吃', () => {
    const text = '开头。\n\n> ⚠️ **注意**: 这里有个坑\n\n结尾还有正文。'

    // 全角/半角都算：`**` 后面必须是 `: ` 才可能是提示块，而这里后面还有空行与正文，
    // 单行匹配在换行处就断掉，于是整段原样保留
    expect(stripNotices(text)).toBe(text)
  })
})
