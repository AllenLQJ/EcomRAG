import { describe, expect, it } from 'vitest';
import { SSEParser, type SSEFrame } from './sse';

describe('SSE parser', () => {
  it('handles every chunk boundary, CRLF, multiline data, comments and unknown fields', () => {
    const wire = ': ping\r\nevent: delta\r\nid: ignored\r\ndata: {"text":\r\ndata: "你好"}\r\n\r\nevent: done\ndata: {}\n\n';
    for (let split = 0; split <= wire.length; split += 1) {
      const frames: SSEFrame[] = [];
      const parser = new SSEParser(frame => frames.push(frame));
      parser.push(wire.slice(0, split));
      parser.push(wire.slice(split));
      parser.finish();
      expect(frames).toEqual([{ event: 'delta', data: '{"text":\n"你好"}' }, { event: 'done', data: '{}' }]);
    }
  });

  it('parses one character at a time and ignores comments without dispatching', () => {
    const frames: SSEFrame[] = [];
    const parser = new SSEParser(frame => frames.push(frame));
    for (const char of ': ping\n\nevent: meta\ndata: {}\n\n') parser.push(char);
    expect(frames).toEqual([{ event: 'meta', data: '{}' }]);
  });

  it('does not dispatch an incomplete event at EOF', () => {
    const frames: SSEFrame[] = [];
    const parser = new SSEParser(frame => frames.push(frame));
    parser.push('event: delta\ndata: {"text":"unfinished"}');
    parser.finish();
    expect(frames).toEqual([]);
  });
});
