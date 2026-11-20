// The scanner's state files: missing is empty, unreadable stops with the path and is left alone,
// saving never leaves half a file (2026-11-20 refactor).
import { afterAll, test } from 'vitest';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { JsonFile, StateFileError, listOf, objectOf, writeAtomic } from './state';

// One folder for this file's tests, removed afterwards (only this folder, which the tests made).
const folder = mkdtempSync(join(tmpdir(), 'trip-bot-state-test-'));
afterAll(() => rmSync(folder, { recursive: true, force: true }));

type Watch = { id: number };
const watches = (name: string) => new JsonFile<Watch[]>(join(folder, name), () => [], listOf<Watch>('盯價行程'));

test('a missing file is the empty default', () => {
  assert.deepEqual(watches('none.json').read(), []);
  assert.equal(watches('none.json').readSaved(), null);
});

test('a saved value reads back', () => {
  const file = watches('saved.json');
  file.write([{ id: 1 }, { id: 2 }]);
  assert.deepEqual(file.read(), [{ id: 1 }, { id: 2 }]);
  assert.deepEqual(file.update(list => list.filter(watch => watch.id !== 1)), [{ id: 2 }]);
  assert.deepEqual(file.read(), [{ id: 2 }]);
});

test('half a file stops with its path and is left as it is', () => {
  const path = join(folder, 'half.json');
  writeFileSync(path, '[{"id": 1}, {"id"');
  assert.throws(() => watches('half.json').read(), (error: unknown) =>
    error instanceof StateFileError && error.path === path && /不是完整的 JSON/.test(error.message) && /沒有改動/.test(error.message));
  assert.equal(readFileSync(path, 'utf8'), '[{"id": 1}, {"id"');
});

test('a file of the wrong shape is unreadable too', () => {
  writeFileSync(join(folder, 'shape.json'), '{"id": 1}');
  assert.throws(() => watches('shape.json').read(), /應該是盯價行程的清單/);
  const settings = new JsonFile<Record<string, unknown>>(join(folder, 'settings.json'), () => ({}), objectOf('設定'));
  writeFileSync(join(folder, 'settings.json'), '[1, 2]');
  assert.throws(() => settings.read(), /應該是一個設定物件/);
});

test('the old behaviour is gone: an unreadable watch list is not saved over', () => {
  const path = join(folder, 'trip-watches.json');
  writeFileSync(path, '[{"id": 7, "city": "Osaka"');
  const file = watches('trip-watches.json');
  assert.throws(() => file.update(list => [...list, { id: 8 }]), StateFileError);
  assert.equal(readFileSync(path, 'utf8'), '[{"id": 7, "city": "Osaka"');
});

test('saving leaves no temporary file and makes the folder', () => {
  const path = join(folder, 'deep', 'er', 'file.json');
  writeAtomic(path, '{}\n');
  assert.equal(readFileSync(path, 'utf8'), '{}\n');
  assert.deepEqual(readdirSync(join(folder, 'deep', 'er')), ['file.json']);
  writeAtomic(path, '{"again": true}\n');
  assert.equal(readFileSync(path, 'utf8'), '{"again": true}\n');
  assert.ok(!existsSync(`${path}.${process.pid}.tmp`));
});
