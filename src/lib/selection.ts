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
