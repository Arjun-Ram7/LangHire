/**
 * The urls between two rows of an ordered list, both ends included, whichever is first.
 * Shift-clicking a checkbox selects everything from the last one clicked to this one, so 130
 * jobs are a click, a scroll and a shift-click instead of 130 clicks.
 */
export function urlsBetween(order: string[], from: string, to: string): string[] {
  const start = order.indexOf(from);
  const end = order.indexOf(to);
  if (start === -1 || end === -1) return [to];
  const [low, high] = start <= end ? [start, end] : [end, start];
  return order.slice(low, high + 1);
}

/**
 * The urls of rows `from` to `to` (1-based, inclusive) of an ordered list, top to bottom.
 * Either end may be given first, and a range past the end stops at the last row, so asking for
 * rows 1 to 130 of a 90-row list selects all 90. Anything that is not a number selects nothing.
 */
export function urlsInRows(order: string[], from: number, to: number): string[] {
  if (!Number.isFinite(from) || !Number.isFinite(to) || order.length === 0) return [];
  const [low, high] = from <= to ? [from, to] : [to, from];
  const start = Math.max(1, Math.floor(low));
  const end = Math.min(order.length, Math.floor(high));
  return start > end ? [] : order.slice(start - 1, end);
}
