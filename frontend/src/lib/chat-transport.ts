/**
 * 对话传输层（R-4 拆分之一）。
 *
 * 变化理由：wire protocol。这里不碰内存状态、不碰 IndexedDB，只负责
 * 「把一轮对话发出去，把事件流读回来」。改动前这些与 run 状态机、持久化、
 * 会话列表通知全挤在 `chat-runs.js` 一个 368 行文件里。
 */

import { TRANSPORT_INCOMPLETE, type ChatEvent, type TransportIncompleteEvent } from '../types';

const FRAME_SEPARATOR = /\r?\n\r?\n/;
const LINE_SEPARATOR = /\r?\n/;
const DATA_PREFIX = 'data:';

export interface StreamChatParams {
  model: string | null;
  reasoningEffort: string | null;
  messages: Array<{ role: string; content: string }>;
  signal: AbortSignal;
}

type ParseErrorHandler = (error: unknown, payload: string) => void;

/**
 * 从错误响应里提取可读说明。
 *
 * 后端对超长请求体返回 422，detail 里写明是哪一项超限。不把它带出来的话，
 * 用户只看到「HTTP 错误: 422」，无从知道该改什么。
 */
async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;

    if (typeof detail === 'string') return detail;

    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown; msg?: unknown } | undefined;
      // loc 形如 ['body', 'messages', 0, 'content']，去掉 'body' 后就是用户能认的路径
      const where = Array.isArray(first?.loc) ? first.loc.slice(1).join('.') : '';
      const message = typeof first?.msg === 'string' ? first.msg : '';
      return where ? `${where}: ${message}` : message;
    }

    return '';
  } catch {
    // 响应体不是 JSON（例如反向代理返回 HTML 错误页）：退回状态码
    return '';
  }
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

  try {
    return JSON.parse(payload) as ChatEvent;
  } catch (error) {
    // 丢掉一帧意味着用户会看到一段内容缺口，必须让调用方知道
    onParseError(error, payload);
    return undefined;
  }
}

async function* parseEventStream(
  body: ReadableStream<Uint8Array>,
  onParseError: ParseErrorHandler,
): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split(FRAME_SEPARATOR);
    // 最后一段可能是不完整的帧，留到下一轮
    buffer = frames.pop() ?? '';

    for (const frame of frames) {
      const event = parseFrame(frame, onParseError);
      if (event !== undefined) yield event;
    }
  }

  // 流结束时缓冲区可能还剩最后一帧（服务端没有以空行收尾）
  const tail = parseFrame(buffer, onParseError);
  if (tail !== undefined) yield tail;
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
  signal,
}: StreamChatParams): AsyncGenerator<ChatEvent> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model,
      reasoning_effort: reasoningEffort,
      messages,
    }),
    signal,
  });

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail ? `HTTP ${response.status}: ${detail}` : `HTTP 错误: ${response.status}`);
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
