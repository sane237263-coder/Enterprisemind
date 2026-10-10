import test from 'node:test';
import assert from 'node:assert/strict';
import { EventStreamDecoder } from '../src/shared/sse.ts';
import { relativeBox } from '../src/features/preview/geometry.ts';

test('Chinese SSE with CRLF survives every possible network split', () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\nevent: answer.delta\r\ndata: {"text":"资料与来源"}\r\n\r\nevent: run.completed\r\ndata: {"id":"9"}\r\n\r\n');
  for (let split = 0; split <= bytes.length; split++) {
    const frames = []; const decoder = new EventStreamDecoder();
    const receive = (event, data) => frames.push([event, data]);
    decoder.push(bytes.slice(0, split), false, receive); decoder.push(bytes.slice(split), true, receive);
    assert.deepEqual(frames, [['answer.delta', { text: '资料与来源' }], ['run.completed', { id: '9' }]]);
  }
});
test('multiple LF events, data lines and unterminated frames', () => {
  const frames = []; const decoder = new EventStreamDecoder();
  const bytes = new TextEncoder().encode('event: step.updated\ndata: {\ndata: "step":"检索"}\n\nevent: answer.delta\ndata: {"text":"unfinished"}');
  for (const byte of bytes) decoder.push(Uint8Array.of(byte), false, (event, data) => frames.push([event, data]));
  decoder.push(undefined, true, (event, data) => frames.push([event, data]));
  assert.deepEqual(frames, [['step.updated', { step: '检索' }]]);
});
test('embedded image boxes use the image origin, not the full slide', () => {
  assert.deepEqual(relativeBox({ x: 110, y: 70, width: 50, height: 20 }, { x: 100, y: 50, width: 200, height: 100 }),
    { x: 5, y: 20, width: 25, height: 20 });
});
test('boxes crossing the image boundary are clipped; invalid boxes are omitted', () => {
  const bounds = { x: 100, y: 50, width: 200, height: 100 };
  assert.deepEqual(relativeBox({ x: 50, y: 20, width: 100, height: 80 }, bounds), { x: 0, y: 0, width: 25, height: 50 });
  assert.equal(relativeBox({ x: 0, y: 0, width: 20, height: 20 }, bounds), undefined);
  assert.equal(relativeBox({ x: NaN, y: 0, width: 20, height: 20 }, bounds), undefined);
});
