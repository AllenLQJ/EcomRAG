export interface SSEFrame { event: string; data: string }

/** Incremental line parser. A trailing CR is held until the next chunk (CRLF may split). */
export class SSEParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];
  constructor(private readonly receive: (frame: SSEFrame) => void) {}

  push(text: string): void {
    this.buffer += text;
    while (true) {
      const match = /[\r\n]/.exec(this.buffer);
      if (!match) return;
      const at = match.index;
      if (this.buffer[at] === '\r' && at === this.buffer.length - 1) return;
      const line = this.buffer.slice(0, at);
      const size = this.buffer[at] === '\r' && this.buffer[at + 1] === '\n' ? 2 : 1;
      this.buffer = this.buffer.slice(at + size);
      this.line(line);
    }
  }

  finish(): void {
    if (this.buffer.endsWith('\r')) this.push('\n');
    // SSE requires a blank line to dispatch. An unterminated final event is discarded.
    this.buffer = '';
  }

  private line(line: string): void {
    if (line === '') {
      const frame = this.data.length ? { event: this.event || 'message', data: this.data.join('\n') } : null;
      this.event = '';
      this.data = [];
      if (frame) this.receive(frame);
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    if (field === 'data') this.data.push(value);
  }
}
