/**
 * 对话传输层（R-4 拆分之一）。
 *
 * 变化理由：wire protocol。这里不碰内存状态、不碰 IndexedDB，只负责
 * 「把一轮对话发出去，把事件流读回来」。改动前这些与 run 状态机、持久化、
 * 会话列表通知全挤在 `chat-runs.js` 一个 368 行文件里。
 */

import { TRANSPORT_INCOMPLETE, type ChatEvent, type TransportIncompleteEvent } from '../types';
import { apiFetch, readErrorDetail } from './api';
import { ChatRequestError } from './chat-request-error';

const FRAME_SEPARATOR = /\r?\n\r?\n/;
const LINE_SEPARATOR = /\r?\n/;
const DATA_PREFIX = 'data:';

/**
 * 空闲看门狗阈值：连续这么久没收到**任何字节**就认定连接已半开。
 *
 * 依据：sse-starlette 默认每 15s 发一条 `: ping` 注释帧，活着的连接不会真的静默
 * 这么久（注释帧同样是字节，同样喂狗）；反过来，断网无 FIN 或上游卡死时，用户
 * 不该对着「生成中」干等——到点中止请求并按不完整收尾。
 */
export const IDLE_TIMEOUT_MS = 60_000;

export interface StreamChatParams {
  model: string | null;
  reasoningEffort: string | null;
  messages: Array<{ role: string; content: string }>;
  /**
   * 上一轮的真实用量锚点（可选）。服务端拿它把上下文预算从「估算整份历史」缩成
   * 「真值 + 估算新增消息」。没有（首轮、或上游没回用量）就不发这个字段。
   */
  contextAnchor?: { messageIndex: number; contextTokens: number } | null;
  signal: AbortSignal;
}

type ParseErrorHandler = (error: unknown, payload: string) => void;

/**
 * 载荷形状校验。
 *
 * `JSON.parse(...) as ChatEvent` 是零运行时保证的断言：`data: null` 满足
 * `!== undefined` 被原样 yield，紧接着 `event.type` 抛 TypeError，整条流断在用户
 * 眼前的「请求失败: Cannot read properties of null」。这里只要求「对象 + type 是
 * 字符串」，不枚举具体类型：服务端将来新增事件类型时按未知事件忽略（chat-store
 * 的 default 分支），不因为版本错位就把连接判死。
 */
function isChatEventShape(value: unknown): value is ChatEvent {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  return typeof (value as { type?: unknown }).type === 'string';
}

/**
 * 解析一个 SSE 帧。
 *
 * 按规范一个帧内可以有多行 `data:`，它们要用换行拼接后再整体解析。
 */
function parseFrame(frame: string, onParseError: ParseErrorHandler): ChatEvent | undefined {
  if (!frame.trim()) return undefined;

  const dataLines: string[] = [];
  for (const line of frame.split(LINE_SEPARATOR)) {
    if (line.startsWith(DATA_PREFIX)) dataLines.push(line.slice(DATA_PREFIX.length).trimStart());
  }
  if (dataLines.length === 0) return undefined;

  const payload = dataLines.join('\n');
  if (!payload) return undefined;

  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    // 丢掉一帧意味着用户会看到一段内容缺口，必须让调用方知道
    onParseError(error, payload);
    return undefined;
  }

  if (!isChatEventShape(parsed)) {
    // 与解析失败同等对待：内容已经缺了一块，而且下一行 `event.type` 会直接抛错
    onParseError(new Error('SSE 载荷不是事件对象'), payload);
    return undefined;
  }
  return parsed;
}

/**
 * 读事件流：分帧 → 产出事件。
 *
 * 三件事保证「读不完也不会挂着」：
 *   - 每次 read 之前重新计时，因此收到任何字节（含心跳注释帧）都重置空闲看门狗；
 *   - 流结束时 flush 一次 decoder：截断在多字节序列中间的半个字不会被静默吞掉；
 *   - 提前退出（调用方 break / 循环体抛错 / 空闲超时）时 cancel 响应体——只
 *     releaseLock 不会关闭底层连接，浏览器会继续收，后端也会继续生成并计费。
 */
async function* parseEventStream(
  body: ReadableStream<Uint8Array>,
  onParseError: ParseErrorHandler,
): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let reachedEnd = false;
  let idleTimedOut = false;

  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  const clearIdleTimer = () => {
    if (idleTimer !== null) clearTimeout(idleTimer);
    idleTimer = null;
  };

  try {
    while (true) {
      // 看门狗与 read 竞速：signal 触发 abort 时 read 自己会 reject，但「半开连接」
      // 什么都不会发生——必须由我们把这次等待切开。计时器只在等 read 的这段时间
      // 里走：数据一到就停表，消费者处理（yield 之后）不该被算成空闲。
      let fireIdle: (() => void) | null = null;
      const idleSignal = new Promise<void>((resolve) => {
        fireIdle = resolve;
      });
      idleTimer = setTimeout(() => fireIdle?.(), IDLE_TIMEOUT_MS);

      const read = await Promise.race([
        reader.read().then((result) => ({ idle: false as const, result })),
        idleSignal.then(() => ({ idle: true as const })),
      ]);
      clearIdleTimer();

      if (read.idle) {
        idleTimedOut = true;
        break;
      }

      const { done, value } = read.result;
      if (done) {
        reachedEnd = true;
        break;
      }

      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split(FRAME_SEPARATOR);
      // 最后一段可能是不完整的帧，留到下一轮
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        const event = parseFrame(frame, onParseError);
        if (event !== undefined) yield event;
      }
    }

    if (!idleTimedOut) {
      // 流结束：flush 掉 decoder 里可能残留的半个多字节序列，它变成 U+FFFD 交给
      // 解析器诊断，而不是无声消失（缓冲区也可能还剩没以空行收尾的最后一帧）。
      buffer += decoder.decode();
      const tail = parseFrame(buffer, onParseError);
      if (tail !== undefined) yield tail;
    }
  } finally {
    clearIdleTimer();
    if (!reachedEnd) {
      // cancel 会取消响应体，浏览器据此关闭连接、服务端停止生成。读完整条流时不必
      // 再 cancel；已被 abort 的流上 cancel 会以 AbortError reject，吞掉是刻意的：
      // 该上抛的异常已经在栈上，清理动作不能把它盖住。
      try {
        await reader.cancel();
      } catch {
        /* 清理期的报错没有信息量 */
      }
    }
    reader.releaseLock();
  }
}

/**
 * 把失败响应翻译成给用户看的错误。
 *
 * 429 单独处理：限流是**有恢复时间**的失败，用户需要知道等多久（后端在
 * `Retry-After` 里给了秒数），而不是一句「HTTP 错误: 429」。
 */
async function requestError(response: Response): Promise<ChatRequestError> {
  if (response.status === 429) {
    const seconds = parseRetryAfterSeconds(response.headers.get('Retry-After'));
    const message =
      seconds === null ? '请求过于频繁，请稍后重试' : `请求过于频繁，请 ${seconds} 秒后重试`;
    return new ChatRequestError(response.status, message, seconds);
  }

  const detail = await readErrorDetail(response);
  return new ChatRequestError(response.status, detail);
}

/** `Retry-After` 的秒数形式（HTTP-date 形式网关不用，解析不出就退回「稍后」）。 */
function parseRetryAfterSeconds(header: string | null): number | null {
  if (header === null) return null;
  const seconds = Number.parseInt(header.trim(), 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

/**
 * 发起一轮对话并产出事件流。
 *
 * 产出服务端事件原样，外加一个客户端合成的 `transport_incomplete`（若缺少 `done`）。
 */
export async function* streamChat({
  model,
  reasoningEffort,
  messages,
  contextAnchor = null,
  signal,
}: StreamChatParams): AsyncGenerator<ChatEvent> {
  // 走 apiFetch：带上 cookie，并在会话过期（401）时通知全局退回登录页
  const response = await apiFetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model,
      reasoning_effort: reasoningEffort,
      messages,
      // 线上字段名用 snake_case：与 messages / reasoning_effort 保持一致
      ...(contextAnchor
        ? {
            context_anchor: {
              message_index: contextAnchor.messageIndex,
              context_tokens: contextAnchor.contextTokens,
            },
          }
        : {}),
    }),
    signal,
  });

  if (!response.ok) {
    throw await requestError(response);
  }
  if (!response.body) {
    throw new Error('响应没有可读的流式主体');
  }

  let sawTerminal = false;
  let damaged = false;

  for await (const event of parseEventStream(response.body, (error, payload) => {
    // 丢掉一帧意味着用户会看到一段内容缺口，必须留下可诊断的痕迹
    damaged = true;
    console.error('SSE JSON 解析错误:', error, '原始数据:', payload);
  })) {
    // `done` 与 `error` 都是契约里的合法终止事件
    if (event.type === 'done' || event.type === 'error') sawTerminal = true;
    yield event;
  }

  // 没干净结束、或中途丢过帧（即使后文补上了 done）都要告知：
  // 后者是静默的内容缺损，不报就是让用户在不知情的情况下读到残缺回复。
  if (!sawTerminal || damaged) {
    const incomplete: TransportIncompleteEvent = { type: TRANSPORT_INCOMPLETE, damaged };
    yield incomplete;
  }
}
