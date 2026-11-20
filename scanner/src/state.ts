// The scanner's own small files in data/: settings, the last lists the user saw, watched trips and the
// background jobs' state. One reader and writer for all of them (2026-11-20 refactor).
//
// Before, each file had its own `try { JSON.parse(...) } catch { return empty }`. A file that could not
// be read looked empty, and the next save wrote over it: a half-written trip-watches.json would have
// dropped every watched trip without a word. Now:
//   - a missing file is the empty default (nothing saved yet);
//   - a file that is there but unreadable stops the command with its path, and is left as it is;
//   - saving writes a temporary file beside it and renames it over the old one, so a crash mid-write
//     leaves the old file, never half of the new one.
// Caches (data/cache, hotel profiles) are different: an unreadable entry is just fetched again.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** data/ beside src/, or TRIP_BOT_DATA (tests run commands against a folder of their own). */
export const DATA_DIR = process.env.TRIP_BOT_DATA ?? fileURLToPath(new URL('../data/', import.meta.url));

export class StateFileError extends Error {
  constructor(readonly path: string, reason: string) {
    super(`${path} 讀不懂（${reason}）。檔案留在原處沒有改動；修好或移走後再試。`);
    this.name = 'StateFileError';
  }
}

/** Write a file in one step: a temporary file beside it, renamed over the old one. */
export function writeAtomic(path: string, text: string): void {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, text);
  renameSync(temporary, path);
}

const missing = (error: unknown) => (error as NodeJS.ErrnoException | null)?.code === 'ENOENT';

/**
 * One JSON file of the scanner's state. `check` returns the value when its shape is right, else a
 * reason it is not, so a file of the wrong shape counts as unreadable too.
 */
export class JsonFile<T> {
  constructor(readonly path: string, private readonly empty: () => T, private readonly check: (value: unknown) => T | string) {}

  /** The saved value; the empty default when there is no file yet. */
  read(): T {
    let text: string;
    try { text = readFileSync(this.path, 'utf8'); } catch (error) {
      if (missing(error)) return this.empty();
      throw new StateFileError(this.path, error instanceof Error ? error.message : String(error));
    }
    let value: unknown;
    try { value = JSON.parse(text); } catch (error) {
      throw new StateFileError(this.path, `不是完整的 JSON：${error instanceof Error ? error.message : String(error)}`);
    }
    const checked = this.check(value);
    if (typeof checked === 'string') throw new StateFileError(this.path, checked);
    return checked;
  }

  /** The saved value, or null when there is no file yet (for "you have not asked for one yet" messages). */
  readSaved(): T | null {
    try { readFileSync(this.path); } catch (error) { if (missing(error)) return null; }
    return this.read();
  }

  write(value: T, pretty = true): void {
    writeAtomic(this.path, `${pretty ? JSON.stringify(value, null, 2) : JSON.stringify(value)}\n`);
  }

  /** Read, change and save in one go; returns what was saved. */
  update(change: (value: T) => T): T {
    const next = change(this.read());
    this.write(next);
    return next;
  }
}

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
/** A check for a JSON object; the fields are the caller's to read defensively. */
export const objectOf = <T>(what: string) => (value: unknown): T | string => isObject(value) ? value as T : `應該是一個${what}物件`;
/** A check for a JSON array of objects. */
export const listOf = <T>(what: string) => (value: unknown): T[] | string =>
  Array.isArray(value) && value.every(isObject) ? value as T[] : `應該是${what}的清單`;

export const dataFile = (name: string) => join(DATA_DIR, name);
