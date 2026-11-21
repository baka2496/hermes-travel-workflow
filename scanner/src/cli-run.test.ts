// The command line run for real against a data folder of its own (TRIP_BOT_DATA, TRIP_BOT_DB): the
// commands that need no web page. Added with the 2026-11-20 refactor of cli.ts.
import { afterAll, test } from 'vitest';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const scanner = fileURLToPath(new URL('..', import.meta.url));
const tsx = join(scanner, 'node_modules', 'tsx', 'dist', 'cli.mjs');
// One folder for this file's runs, removed afterwards (only this folder, which the tests made).
const folder = mkdtempSync(join(tmpdir(), 'trip-bot-cli-test-'));
afterAll(() => rmSync(folder, { recursive: true, force: true }));

function deals(data: string, ...args: string[]) {
  const run = spawnSync(process.execPath, [tsx, join(scanner, 'src', 'cli.ts'), ...args], {
    cwd: scanner, encoding: 'utf8', timeout: 60_000,
    env: { ...process.env, TRIP_BOT_DATA: data, TRIP_BOT_DB: join(data, 'trip-bot.db') },
  });
  return { status: run.status, stdout: run.stdout.trim(), stderr: run.stderr.trim() };
}
const fresh = (name: string) => mkdtempSync(join(folder, `${name}-`));

test('settings: party size and preferences are saved and read back', { timeout: 60_000 }, () => {
  const data = fresh('settings');
  assert.match(deals(data, 'travelers').stdout, /目前設定 1 人同行（1 間房）/);
  assert.match(deals(data, 'travelers', '3').stdout, /已設定 3 人同行/);
  assert.match(deals(data, 'travelers').stdout, /目前設定 3 人同行（2 間房）/);
  assert.match(deals(data, 'prefs', 'minRating', '4.2').stdout, /評分 4\.2 以上/);
  const saved = JSON.parse(readFileSync(join(data, 'settings.json'), 'utf8'));
  assert.deepEqual(saved, { travelers: 3, prefs: { minRating: 4.2 } });
});

test('nothing saved yet: the commands say so', { timeout: 60_000 }, () => {
  const data = fresh('empty');
  assert.match(deals(data, 'watches').stdout, /目前沒有在盯的行程/);
  assert.match(deals(data, 'report').stdout, /目前沒有啟用中的掃描地區/);
  const rooms = deals(data, 'rooms', '1');
  assert.equal(rooms.status, 1);
  assert.match(rooms.stderr, /還沒有住宿清單/);
  assert.match(deals(data, 'hotels-for', 'T1').stderr, /還沒有指定日期的行程/);
});

test('an unreadable watch list stops the command and is not written over', { timeout: 60_000 }, () => {
  const data = fresh('broken');
  const path = join(data, 'trip-watches.json');
  writeFileSync(path, '[{"id": 1, "city": "Osaka"');
  for (const args of [['watches'], ['unwatch', '1']]) {
    const run = deals(data, ...args);
    assert.equal(run.status, 1, args.join(' '));
    assert.match(run.stderr, /trip-watches\.json 讀不懂/);
  }
  assert.equal(readFileSync(path, 'utf8'), '[{"id": 1, "city": "Osaka"');
});

test('an unreadable settings file is reported, not taken as one traveller', { timeout: 60_000 }, () => {
  const data = fresh('settings-broken');
  writeFileSync(join(data, 'settings.json'), '{"travelers": 2,');
  const run = deals(data, 'travelers');
  assert.equal(run.status, 1);
  assert.match(run.stderr, /settings\.json 讀不懂/);
});

test('--check keeps the message for the rule check and prints only the verdict', { timeout: 60_000 }, () => {
  const data = fresh('check');
  const run = deals(data, 'watches', '--check');
  assert.equal(run.stdout, '✓');
  assert.equal(run.status, 0);
});

// Another agent's Kanban card asks for prices with `plan` (2026-11-21: a card answered from airline home
// pages because `trip` replaces the user's Telegram list). Bad input stops before any lookup.
test('plan: checks the city, dates and party size before looking anything up', { timeout: 60_000 }, () => {
  const data = fresh('plan');
  for (const args of [['plan'], ['plan', '大阪', '2027-01-24', '2027-01-19'], ['plan', '大阪', '2020-01-19', '2020-01-24'],
    ['plan', '大阪', '2027-01-19', '2027-01-24', '0'], ['plan', '大阪', '2027-01-19', '2027-01-24', 'two']]) {
    const run = deals(data, ...args);
    assert.equal(run.status, 1, args.join(' '));
    assert.match(run.stderr, /用法：plan|出發日已經過了|人數請用 1 到 9/, args.join(' '));
  }
  assert.deepEqual(readdirSync(data).filter(name => /^last-/.test(name)), []);
});

// What `plan` must leave alone: the user's T1, hotel list and buttons, and the saved party size.
test('plan saves no list, buttons or party size', () => {
  const source = readFileSync(new URL('./cli.ts', import.meta.url), 'utf8');
  const start = source.indexOf('async function planForCard');
  const body = source.slice(start, source.indexOf('\n}\n', start));
  assert.ok(start > 0);
  for (const writer of ['lastTripFile.write', 'saveListedHotels', 'settingsFile.write', 'lastHotelsFile.write']) assert.ok(!body.includes(writer), writer);
  assert.match(body, /out\.keyboard = \[\]/);
  assert.match(source, /command === 'plan'\) await planForCard\(/);
  assert.doesNotMatch(source.slice(source.indexOf("command === 'plan'"), source.indexOf("command === 'watch'")), /saveListedHotels/);
});

// 2026-11-21: a card added the bags to a total that already held them (NT$35,825 → NT$39,425).
test('plan says its totals already hold the bags', () => {
  const source = readFileSync(new URL('./cli.ts', import.meta.url), 'utf8');
  const start = source.indexOf('async function planForCard');
  assert.match(source.slice(start, source.indexOf('\n}\n', start)), /say\(PLAN_TOTAL_NOTE\)/);
  assert.ok(source.includes('PLAN_TOTAL_NOTE = ') && source.includes('不要再加'));
  // 2026-11-21: no bag line, and the user was not told the total left the bags out.
  assert.ok(source.includes('總價沒含托運行李') && source.includes('未含托運行李'));
});

// 2026-11-21: plan read no flights once and the card said 「遠期尚未開賣」 with hotels nobody looked up.
test('plan says no flights read is not "not on sale" and hotels are unchecked', () => {
  const source = readFileSync(new URL('./cli.ts', import.meta.url), 'utf8');
  const start = source.indexOf('async function planForCard');
  const body = source.slice(start, source.indexOf('\n}\n', start));
  const noFlights = body.slice(body.indexOf('!found?.flights.length'), body.indexOf('return;'));
  assert.match(noFlights, /say\(PLAN_NO_FLIGHTS_NOTE\)/);
  assert.ok(source.includes('不是還沒開賣') && source.includes('不要自己挑飯店'));
});
