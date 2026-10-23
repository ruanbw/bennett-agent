import { describe, expect, it } from 'vitest';

import {
  classifyActivity,
  doneProcessTitle,
  formatRunDuration,
  formatRunDurationText,
  processTitle,
  rankActivities,
  runningProcessTitle,
  summarizeActivities,
  toolRowTitle,
  turnStatusLabel,
  type ActivityKind,
} from './step-process';

describe('工具名 → 动作分类', () => {
  it('完整覆盖 DSH 的判定表', () => {
    expect(classifyActivity('read')).toBe('read');
    expect(classifyActivity('read_image')).toBe('readImage');
    expect(classifyActivity('grep')).toBe('search');
    expect(classifyActivity('glob')).toBe('search');
    expect(classifyActivity('cordis_runtime_inspect')).toBe('search');
    expect(classifyActivity('write')).toBe('write');
    expect(classifyActivity('edit')).toBe('edit');
    expect(classifyActivity('apply_patch')).toBe('edit');
    expect(classifyActivity('bash')).toBe('commands');
    expect(classifyActivity('pwsh')).toBe('commands');
    expect(classifyActivity('exec_command')).toBe('commands');
    expect(classifyActivity('write_stdin')).toBe('commands');
    expect(classifyActivity('terminal_read')).toBe('commands');
    expect(classifyActivity('run_code')).toBe('code');
    expect(classifyActivity('web_search')).toBe('webSearch');
    expect(classifyActivity('web_fetch')).toBe('webFetch');
    expect(classifyActivity('subagent')).toBe('subagents');
    expect(classifyActivity('subagent_fork')).toBe('subagents');
    expect(classifyActivity('todo_write')).toBe('plan');
    expect(classifyActivity('create_goal')).toBe('plan');
    expect(classifyActivity('update_goal')).toBe('plan');
    expect(classifyActivity('get_goal')).toBe('plan');
    expect(classifyActivity('ask_user_question')).toBe('questions');
    expect(classifyActivity('request_user_input')).toBe('questions');
    expect(classifyActivity('some_unknown_tool')).toBe('tools');
  });

  it('认本仓自己的工具名（后端 app/tools/ 的那几个）', () => {
    expect(classifyActivity('tavily_search')).toBe('webSearch');
    expect(classifyActivity('ask_user_clarification')).toBe('questions');
    expect(classifyActivity('run_command')).toBe('commands');
    expect(classifyActivity('execute_command')).toBe('commands');
    expect(classifyActivity('shell')).toBe('commands');
  });

  it('知识库检索不冒充「搜索网页」：它并没有上网', () => {
    // 归到 webSearch 会让组头写「已搜索网页」，这是谎报。它有自己的一类
    // （「检索了知识库」），比泛泛的「已调用工具」信息量大。
    expect(classifyActivity('search_knowledge_base')).toBe('knowledge');
    expect(classifyActivity('knowledge_search')).toBe('knowledge');
  });

  it('查时间有自己的类别：这是默认装配里最常被调用的工具之一', () => {
    expect(classifyActivity('get_current_time')).toBe('time');
    expect(doneProcessTitle('time')).toBe('查询了时间');
  });

  it('MCP 工具取最后一段再判定，大小写不敏感', () => {
    expect(classifyActivity('mcp__fs__read_file')).toBe('read');
    expect(classifyActivity('mcp__fs__read')).toBe('read');
    expect(classifyActivity('mcp__github__web_search')).toBe('webSearch');
    expect(classifyActivity('BASH')).toBe('commands');
  });

  it('没登记的 MCP 工具按动词前缀兜底，仍比「已调用工具」有信息量', () => {
    expect(classifyActivity('mcp__fs__list_directory')).toBe('search');
    expect(classifyActivity('mcp__db__query_rows')).toBe('search');
    // 认不出的动词仍然老实说「已调用工具」，不瞎猜
    expect(classifyActivity('mcp__x__dispatch_job')).toBe('tools');
  });

  it('精确判定优先于前缀兜底：knowledge_search 不因为 search 前缀被当成代码检索', () => {
    expect(classifyActivity('search_knowledge_base')).toBe('knowledge');
    expect(classifyActivity('knowledge_search')).toBe('knowledge');
  });

  it('空名字不炸，回落成「其它工具」', () => {
    expect(classifyActivity('')).toBe('tools');
    expect(classifyActivity('   ')).toBe('tools');
  });
});

describe('运行中的组头文案', () => {
  it('按当前动作给「正在…」，不是固定的「思考中」', () => {
    const cases: Array<[ActivityKind, string]> = [
      ['thinking', '正在分析请求'],
      ['read', '正在读取文件'],
      ['readImage', '正在读取图片'],
      ['write', '正在写入文件'],
      ['search', '正在搜索代码'],
      ['edit', '正在编辑文件'],
      ['commands', '正在运行命令'],
      ['code', '正在运行代码'],
      ['webSearch', '正在搜索网页'],
      ['webFetch', '正在访问网页'],
      ['subagents', '正在协调子智能体'],
      ['plan', '正在更新计划'],
      ['questions', '等待你的操作'],
      ['tools', '正在调用工具'],
    ];
    for (const [kind, label] of cases) {
      expect(runningProcessTitle(kind)).toBe(label);
    }
  });

  it('调用还没真正发出时用「准备…」变体', () => {
    expect(runningProcessTitle('read', true)).toBe('准备读取文件');
    expect(runningProcessTitle('tools', true)).toBe('准备调用工具');
    expect(runningProcessTitle('webSearch', true)).toBe('准备搜索网页');
    expect(runningProcessTitle('questions', true)).toBe('准备提问');
    // 「准备分析请求」不是中国人会说的话：分析不是一个待发出的调用
    expect(runningProcessTitle('thinking', true)).toBe('准备调用工具');
  });
});

describe('已结束的组头文案', () => {
  it('每类动作都有过去式说法', () => {
    expect(doneProcessTitle('thinking')).toBe('已完成分析');
    expect(doneProcessTitle('read')).toBe('已读取文件');
    expect(doneProcessTitle('readImage')).toBe('已读取图片');
    expect(doneProcessTitle('write')).toBe('已写入文件');
    expect(doneProcessTitle('search')).toBe('已搜索代码');
    expect(doneProcessTitle('edit')).toBe('修改了文件');
    expect(doneProcessTitle('commands')).toBe('执行了命令');
    expect(doneProcessTitle('code')).toBe('运行了代码');
    expect(doneProcessTitle('webSearch')).toBe('已搜索网页');
    expect(doneProcessTitle('webFetch')).toBe('已访问网页');
    expect(doneProcessTitle('subagents')).toBe('已协调子智能体');
    expect(doneProcessTitle('plan')).toBe('更新了计划');
    expect(doneProcessTitle('questions')).toBe('向用户提出了问题');
    expect(doneProcessTitle('tools')).toBe('已调用工具');
  });
});

describe('组标题拼接', () => {
  it('没有工具时只说「已完成分析」，不要空标题', () => {
    expect(processTitle([])).toBe('已完成分析');
  });

  it('单项直接用它自己，不带次数', () => {
    // 强调：调了 5 次也只说「执行了命令」，计数对读者没意义
    expect(processTitle([{ kind: 'commands', count: 5 }])).toBe('执行了命令');
  });

  it('两项用「并」连接，都以「已」开头时第二项去掉「已」', () => {
    expect(
      processTitle([
        { kind: 'read', count: 2 },
        { kind: 'write', count: 1 },
      ])
    ).toBe('已读取文件并写入文件');

    // 第二项不以「已」开头时原样保留：`修改了文件` 去掉首字会变成「改了文件」
    expect(
      processTitle([
        { kind: 'read', count: 2 },
        { kind: 'edit', count: 1 },
      ])
    ).toBe('已读取文件并修改了文件');
  });

  it('三项用「，」连接，不去前缀', () => {
    expect(
      processTitle([
        { kind: 'read', count: 3 },
        { kind: 'commands', count: 2 },
        { kind: 'webSearch', count: 1 },
      ])
    ).toBe('已读取文件，执行了命令，已搜索网页');
  });

  it('超过三类才加「等」，三类以内不加', () => {
    const three = processTitle([
      { kind: 'read', count: 4 },
      { kind: 'write', count: 3 },
      { kind: 'commands', count: 2 },
    ]);
    expect(three).toBe('已读取文件，已写入文件，执行了命令');
    expect(three).not.toContain('等');

    expect(
      processTitle([
        { kind: 'read', count: 4 },
        { kind: 'write', count: 3 },
        { kind: 'commands', count: 2 },
        { kind: 'webSearch', count: 1 },
      ])
    ).toBe('已读取文件，已写入文件，执行了命令等');
  });

  it('按调用次数降序排，用户读到的第一项是干得最多的那件事', () => {
    expect(
      processTitle([
        { kind: 'webSearch', count: 1 },
        { kind: 'read', count: 9 },
      ])
    ).toBe('已读取文件并搜索网页');
  });
});

describe('活动排序', () => {
  it('次数降序，次数相同保持首次出现顺序', () => {
    expect(
      rankActivities([
        { kind: 'webSearch', count: 1 },
        { kind: 'read', count: 1 },
        { kind: 'commands', count: 3 },
      ])
    ).toEqual(['commands', 'webSearch', 'read']);
  });

  it('最多返回三类', () => {
    const ranked = rankActivities([
      { kind: 'read', count: 6 },
      { kind: 'write', count: 5 },
      { kind: 'commands', count: 4 },
      { kind: 'webSearch', count: 3 },
      { kind: 'tools', count: 2 },
    ]);
    expect(ranked).toHaveLength(3);
  });

  it('从工具名列表聚合时保持首次出现顺序、同类累加', () => {
    expect(summarizeActivities(['bash', 'read', 'bash'])).toEqual([
      { kind: 'commands', count: 2 },
      { kind: 'read', count: 1 },
    ]);
    expect(summarizeActivities([])).toEqual([]);
  });
});

describe('时长格式化', () => {
  it('不足一分钟只给秒', () => {
    expect(formatRunDurationText(11_000)).toBe('11秒');
    expect(formatRunDurationText(1_000)).toBe('1秒');
  });

  it('分钟 + 秒，省略前导零', () => {
    expect(formatRunDurationText(3 * 60_000 + 24_000)).toBe('3分24秒');
    // 不写 1分05秒
    expect(formatRunDurationText(65_000)).toBe('1分5秒');
  });

  it('超过一小时带上小时', () => {
    expect(formatRunDurationText(3_600_000)).toBe('1小时0分0秒');
    expect(formatRunDurationText(3_723_000)).toBe('1小时2分3秒');
  });

  it('向下取整到秒：还没到下一档就不提前显示', () => {
    expect(formatRunDurationText(59_900)).toBe('59秒');
    expect(formatRunDurationText(999)).toBe('0秒');
  });

  it('负数/异常按 0 处理，不显示 -3秒', () => {
    expect(formatRunDurationText(-5_000)).toBe('0秒');
    expect(formatRunDurationText(Number.NaN)).toBe('0秒');
  });

  it('数字与单位分开返回，调用方可以只给数字套等宽字体', () => {
    expect(formatRunDuration(83_000)).toEqual([
      { text: '1', numeric: true },
      { text: '分', numeric: false },
      { text: '23', numeric: true },
      { text: '秒', numeric: false },
    ]);
  });
});

describe('异常结束的状态文案', () => {
  it('被停止 / 中断 / 失败各说各的，不再统一成「已停止」', () => {
    expect(turnStatusLabel('aborted')).toBe('已停止');
    expect(turnStatusLabel('interrupted')).toBe('回复不完整（已中断）');
    expect(turnStatusLabel('error')).toBe('处理失败');
  });
});

describe('工具行的标题', () => {
  it('具体工具优先用精修标题（与组头的叙述式文案不同）', () => {
    expect(toolRowTitle('grep')).toBe('搜索文件内容');
    expect(toolRowTitle('glob')).toBe('查找文件');
    expect(toolRowTitle('web_search')).toBe('网页搜索');
    expect(toolRowTitle('read_image')).toBe('读取图片');
    expect(toolRowTitle('todo_write')).toBe('更新任务清单');
  });

  it('没登记的工具退回动作分类的通用标题', () => {
    expect(toolRowTitle('bash')).toBe('运行命令');
    expect(toolRowTitle('read')).toBe('读取');
    expect(toolRowTitle('write')).toBe('写入');
    expect(toolRowTitle('edit')).toBe('编辑');
    expect(toolRowTitle('run_code')).toBe('代码');
  });

  it('认不出时给「工具调用」，不回显内部名字', () => {
    expect(toolRowTitle('some_unknown_tool')).toBe('工具调用');
    expect(toolRowTitle('')).toBe('工具调用');
  });

  it('本仓工具用自己的标题，不复用「网页搜索」冒充联网', () => {
    expect(toolRowTitle('search_knowledge_base')).toBe('检索知识库');
    expect(toolRowTitle('get_current_time')).toBe('查看时间');
    expect(toolRowTitle('ask_user_clarification')).toBe('提问');
  });
});
