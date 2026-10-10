/** Frame boundaries may span arbitrary UTF-8 network chunks. */
export class EventStreamDecoder {
  private decoder = new TextDecoder();
  private buffer = '';
  push(value: Uint8Array | undefined, done: boolean, receive: (event: string, data: Record<string, unknown>) => void) {
    this.buffer += this.decoder.decode(value, { stream: !done });
    let boundary: RegExpExecArray | null;
    while ((boundary = /\r?\n\r?\n/.exec(this.buffer))) {
      const frame = this.buffer.slice(0, boundary.index);
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
      const lines = frame.split(/\r?\n/);
      const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
      const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (event && data) receive(event, JSON.parse(data));
    }
  }
}
