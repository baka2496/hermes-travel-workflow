import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// A throwaway database for each test run; the store reads TRIP_BOT_DB when first imported.
// 2026-11-20: the folder was left behind after every run (21 had piled up); it is removed now,
// only this folder, which this file made.
const folder = mkdtempSync(join(tmpdir(), 'trip-bot-store-test-'));
process.env.TRIP_BOT_DB = join(folder, 'test.db');
let store: typeof import('./store');
let profiles: typeof import('./hotel-profiles');
beforeAll(async () => {
  store = await import('./store');
  profiles = await import('./hotel-profiles');
});
afterAll(() => {
  store.closeStore();
  rmSync(folder, { recursive: true, force: true });
  expect(existsSync(folder)).toBe(false);
});

describe('store', () => {
  it('keeps watches, scans and alerts', () => {
    const watch = store.createWatch({ origin: 'Taipei', originLabel: '台北', region: 'Japan', regionLabel: '日本', tripLength: 'week', sendDigest: true });
    const scannedAt = new Date('2026-10-18T03:00:00Z');
    store.addObservations([
      { watchId: watch.id, city: 'Tokyo', outboundDate: '2026-10-29', returnDate: '2026-11-04', stops: 0, duration: null, price: 6871, currency: 'TWD', scannedAt },
      { watchId: watch.id, city: 'Okinawa', outboundDate: '2026-10-29', returnDate: '2026-11-05', stops: 0, duration: null, price: 4699, currency: 'TWD', scannedAt },
    ]);
    expect(store.observations(watch.id, { scannedAt }).map(row => row.city)).toEqual(['Okinawa', 'Tokyo']);
    expect(store.observations(watch.id, { scannedAt: null })).toEqual([]);
    expect(store.findWatch('Taipei', 'Japan', 'week')).toMatchObject({ id: watch.id, sendDigest: true, active: true, lastScanAt: null });

    const message = { title: '日本促銷', body: '沖繩 NT$4,699', url: '' };
    store.addAlert({ watchId: watch.id, kind: 'deal', city: 'Okinawa', price: 4699, eventKey: 'deal:1', message }, scannedAt);
    store.addAlert({ watchId: watch.id, kind: 'deal', city: 'Okinawa', price: 4699, eventKey: 'deal:1', message }, scannedAt);
    expect(store.dueAlerts(new Date('2026-10-18T04:00:00Z'))).toHaveLength(1);
    expect(store.recentAlerts({ kind: 'deal', since: new Date('2026-10-01'), tripLengths: ['week'] })[0]?.message).toEqual(message);
    expect(store.recentAlerts({ kind: 'deal', since: new Date('2026-10-01'), tripLengths: ['weekend'] })).toEqual([]);
  });

  it('lets only one run claim a due watch', () => {
    const watch = store.createWatch({ origin: 'Taipei', originLabel: '台北', region: 'Korea', regionLabel: '韓國', tripLength: 'weekend' });
    const next = new Date(watch.nextScanAt.getTime() + 6 * 3_600_000);
    expect(store.claimWatch(watch, next)).toBe(true);
    expect(store.claimWatch(watch, next)).toBe(false);
  });

  it('rolls a failed transaction back', () => {
    const watch = store.createWatch({ origin: 'Taipei', originLabel: '台北', region: 'Thailand', regionLabel: '泰國', tripLength: 'week' });
    expect(() => store.transaction(() => {
      store.updateWatch(watch.id, { lastError: 'boom' });
      throw new Error('fail');
    })).toThrow('fail');
    expect(store.findWatch('Taipei', 'Thailand', 'week')?.lastError).toBeNull();
  });

  it('stores hotel profiles in SQLite with countryCode, migrates legacy JSON, and purges wrong-country rows', () => {
    const legacy = join(folder, 'legacy-profiles.json');
    const fetchedAt = new Date().toISOString();
    writeFileSync(legacy, JSON.stringify({
      apaosaka: {
        name: 'APA Hotel Osaka Higobashi Ekimae',
        hotelUrl: 'https://www.booking.com/hotel/jp/apa-osaka-higobashi-ekimae.en-gb.html',
        rooms: [{ name: 'Double Room', beds: ['1 double bed'], guests: 2, bathroom: 'private', photos: [], bunkInPhotos: false }],
        fetchedAt,
      },
      yisuhotel: {
        name: 'Yi Su Hotel',
        hotelUrl: 'https://www.booking.com/hotel/tw/yi-su-taipei-taipei5.en-gb.html',
        rooms: [{ name: 'Double Room', beds: ['1 double bed'], guests: 2, bathroom: 'private', photos: [], bunkInPhotos: false }],
        fetchedAt,
      },
    }));
    const skippedRes = profiles.importLegacyProfiles(legacy, { skipCountries: ['tw'] });
    expect(skippedRes).toEqual({ imported: 1, skipped: 1 });
    expect(store.countHotelProfiles()).toBe(1);
    expect(profiles.freshProfile('APA Hotel Osaka Higobashi Ekimae')?.countryCode).toBe('jp');
    expect(profiles.freshProfile('Yi Su Hotel')).toBeNull();

    // Direct saveProfile tags countryCode automatically and can be purged by country
    profiles.saveProfile({
      name: 'Yi Su Hotel',
      hotelUrl: 'https://www.booking.com/hotel/tw/yi-su-taipei-taipei5.en-gb.html',
      rooms: [],
      fetchedAt,
    });
    expect(store.listHotelProfiles({ countryCode: 'tw' })).toHaveLength(1);
    expect(store.deleteHotelProfilesByCountry('tw')).toBe(1);
    expect(store.listHotelProfiles({ countryCode: 'tw' })).toEqual([]);
  });

  it('stores, lists, and prunes query_cache entries in SQLite', () => {
    const oldIso = '2026-10-01T00:00:00.000Z';
    const freshIso = '2026-11-28T00:00:00.000Z';
    store.setCacheEntry({ cacheKey: 'stays-old', kind: 'stays', city: 'Osaka', countryCode: 'jp', keyJson: '[]', valueJson: '{"picks":[]}', savedAt: oldIso });
    store.setCacheEntry({ cacheKey: 'stays-new', kind: 'stays', city: 'Osaka', countryCode: 'jp', keyJson: '[]', valueJson: '{"picks":[1]}', savedAt: freshIso });
    store.setCacheEntry({ cacheKey: 'flights-new', kind: 'flights', city: 'Osaka', countryCode: 'jp', keyJson: '[]', valueJson: '{}', savedAt: freshIso });

    expect(store.getCacheEntry('stays-new')?.valueJson).toBe('{"picks":[1]}');
    expect(store.listCacheEntries(['stays'], '2026-11-27T00:00:00.000Z').map(e => e.cacheKey)).toEqual(['stays-new']);
    expect(store.listCacheEntries(['stays', 'flights'], '2026-11-27T00:00:00.000Z')).toHaveLength(2);
    expect(store.pruneCacheEntries('2026-11-01T00:00:00.000Z')).toBe(1);
    expect(store.getCacheEntry('stays-old')).toBeNull();
    expect(store.getCacheEntry('stays-new')).not.toBeNull();
  });
});
