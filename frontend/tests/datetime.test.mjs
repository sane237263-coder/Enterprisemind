import test from 'node:test';
import assert from 'node:assert/strict';
import { formatBeijingTime, formatSystemTimes } from '../src/shared/datetime.ts';

test('Beijing time converts UTC and explicit offsets, drops subseconds and crosses dates', () => {
  assert.equal(formatBeijingTime('2026-10-04T11:39:49.723888Z'), '2026-10-04 19:39:49（北京时间）');
  assert.equal(formatBeijingTime('2026-10-04T19:39:49+08:00'), '2026-10-04 19:39:49（北京时间）');
  assert.equal(formatBeijingTime('2026-12-31T16:00:00Z'), '2027-01-01 00:00:00（北京时间）');
  assert.equal(formatBeijingTime('2026-10-04T04:39:49-07:00'), '2026-10-04 19:39:49（北京时间）');
});

test('missing, invalid and timezone-less values never invent a device-local instant', () => {
  for (const value of [undefined, null, '', 'invalid', '2026-10-04', '2026-10-04T11:39:49']) {
    assert.equal(formatBeijingTime(value), '—');
  }
});

test('legacy system timestamps are readable without altering other text or formatting twice', () => {
  const raw = '确认删除“report-2026-10-04T11:39:49Z”？确认有效期至 2026-10-04T11:39:49.723888Z。';
  const expected = '确认删除“report-2026-10-04T11:39:49Z”？确认有效期至 2026-10-04 19:39:49（北京时间）。';
  assert.equal(formatSystemTimes(raw), expected);
  assert.equal(formatSystemTimes(expected), expected);
  assert.equal(formatSystemTimes('查询时间：2026-12-31T16:00:00+00:00。'), '查询时间：2027-01-01 00:00:00（北京时间）。');
});
