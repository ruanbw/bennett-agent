import { describe, expect, it } from 'vitest';
import { parseTodoArgs, todoSummary, deriveLatestTodos } from './tool-todo.aui';

describe('parseTodoArgs', () => {
  it('解析合法快照（整表替换的参数）', () => {
    const args = JSON.stringify({
      todos: [
        { content: '调研方案', status: 'completed' },
        { content: '写实现', status: 'in_progress' },
        { content: '跑测试', status: 'pending' },
      ],
    });
    expect(parseTodoArgs(args)).toEqual({
      todos: [
        { content: '调研方案', status: 'completed' },
        { content: '写实现', status: 'in_progress' },
        { content: '跑测试', status: 'pending' },
      ],
      empty: false,
    });
  });

  it('空清单是合法快照（清空语义），返回 empty 标记', () => {
    expect(parseTodoArgs('{"todos":[]}')).toEqual({ todos: [], empty: true });
  });

  it('流式半截 JSON / 非 JSON 返回 null（面板隐藏而不是显示半份数据）', () => {
    expect(parseTodoArgs('{"todos":[{"cont')).toBeNull();
    expect(parseTodoArgs('')).toBeNull();
    expect(parseTodoArgs(null)).toBeNull();
  });

  it('任何一项不合法整体放弃——半份清单会让人误以为其余任务被丢弃', () => {
    const bad = JSON.stringify({
      todos: [
        { content: '好的一项', status: 'pending' },
        { content: '', status: 'pending' },
      ],
    });
    expect(parseTodoArgs(bad)).toBeNull();
    expect(parseTodoArgs('{"todos":[{"content":"x","status":"done"}]}')).toBeNull();
    expect(parseTodoArgs('{"todos":"nope"}')).toBeNull();
  });

  it('内容按服务端同一口径折叠空白串', () => {
    expect(parseTodoArgs('{"todos":[{"content":"  多  行  ","status":"pending"}]}')).toEqual({
      todos: [{ content: '多 行', status: 'pending' }],
      empty: false,
    });
  });
});

describe('deriveLatestTodos', () => {
  const part = (argsText: string, toolName = 'todo_write') => ({
    type: 'tool-call',
    toolName,
    argsText,
  });

  it('取最新一次 todo_write 的快照（整表替换语义）', () => {
    const messages = [
      {
        parts: [
          part('{"todos":[{"content":"旧任务","status":"pending"}]}'),
          { type: 'text', text: '回答' },
        ],
      },
      {
        parts: [part('{"todos":[{"content":"新任务","status":"in_progress"}]}')],
      },
    ];
    expect(deriveLatestTodos(messages)).toEqual({
      todos: [{ content: '新任务', status: 'in_progress' }],
      empty: false,
    });
  });

  it('最新一次是空清单 = 明确清空，返回 empty 而不是回退旧清单', () => {
    const messages = [
      { parts: [part('{"todos":[{"content":"旧任务","status":"pending"}]}')] },
      { parts: [part('{"todos":[]}')] },
    ];
    expect(deriveLatestTodos(messages)).toEqual({ todos: [], empty: true });
  });

  it('最新一次参数损坏时越过它取上一次可解析的（流式半截 JSON 不至于清掉面板）', () => {
    const messages = [
      { parts: [part('{"todos":[{"content":"完好的旧清单","status":"pending"}]}')] },
      { parts: [part('{"todos":[{"cont')] },
    ];
    expect(deriveLatestTodos(messages)).toEqual({
      todos: [{ content: '完好的旧清单', status: 'pending' }],
      empty: false,
    });
  });

  it('会话从未用过 todo_write → null；兼容 content 形状（落库记录）', () => {
    expect(deriveLatestTodos([])).toBeNull();
    expect(deriveLatestTodos([{ parts: [{ type: 'text', text: 'hi' }] }])).toBeNull();
    const stored = [
      { content: [part('{"todos":[{"content":"a","status":"completed"}]}')] },
    ];
    expect(deriveLatestTodos(stored)).toEqual({
      todos: [{ content: 'a', status: 'completed' }],
      empty: false,
    });
  });
});

describe('todoSummary', () => {
  it('全部完成只报计数', () => {
    expect(
      todoSummary([
        { content: 'a', status: 'completed' },
        { content: 'b', status: 'completed' },
      ]),
    ).toBe('2/2 已完成');
  });

  it('进行中点名第一条活跃任务', () => {
    expect(
      todoSummary([
        { content: 'a', status: 'completed' },
        { content: '写实现', status: 'in_progress' },
        { content: 'c', status: 'pending' },
      ]),
    ).toBe('1/3 已完成 · 写实现');
  });
});
