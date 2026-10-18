import type { ChatStreamEvent } from '../api/types';

export function frame(event: ChatStreamEvent): string {
  return `event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`;
}

export function controlledSSE() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  return {
    response: new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }),
    event(event: ChatStreamEvent) { controller.enqueue(encoder.encode(frame(event))); },
    text(text: string) { controller.enqueue(encoder.encode(text)); },
    close() { controller.close(); },
  };
}
