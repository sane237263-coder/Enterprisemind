import test from 'node:test';
import assert from 'node:assert/strict';
import { LatestRequest } from '../src/shared/latest-request.ts';

test('an older message load cannot overwrite a newer completed transcript', async () => {
  const requests = new LatestRequest();
  let displayed;
  let finishOlder;
  const olderResult = new Promise(resolve => { finishOlder = resolve; });
  async function load(result) {
    const version = requests.begin();
    const messages = await result;
    if (requests.isCurrent(version)) displayed = messages;
  }

  const older = load(olderResult);
  await load(Promise.resolve(['user', 'completed assistant answer']));
  finishOlder(['user']);
  await older;

  assert.deepEqual(displayed, ['user', 'completed assistant answer']);
});
