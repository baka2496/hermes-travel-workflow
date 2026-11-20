// What a command shows the user: its lines, grouped by photo album, and its buttons.
//
// Until 2026-11-20 cli.ts replaced console.log itself in --check mode to catch what the commands
// printed. Now commands call `say()`, and only this object decides whether a line is printed or kept
// for the rule check (src/rules.ts), so nothing else that logs can end up in the checked message.
import type { Segment } from './telegram';

export class Output {
  /** The message in stretches: one with photos goes out as an album captioned by its own text. */
  readonly segments: Segment[] = [{ text: '', photos: [] }];
  /** Button rows under the message. */
  keyboard: string[][] = [];

  /** keep: hold the lines for the rule check instead of printing them. */
  constructor(private readonly keep: boolean, private readonly print: (line: string) => void = line => console.log(line)) {}

  say(...parts: unknown[]): void {
    const line = parts.map(String).join(' ');
    if (!this.keep) { this.print(line); return; }
    const segment = this.segments.at(-1)!;
    segment.text = segment.text ? `${segment.text}\n${line}` : line;
  }

  /** Start a stretch with these photos (own: they belong to this stretch's hotel or room). */
  album(photos: string[] = [], own = false): void {
    this.segments.push({ text: '', photos, own });
  }

  /** Whether anything was said while keeping lines. */
  get said(): boolean {
    return this.segments.some(segment => segment.text);
  }
}
