export type Box = { x: number; y: number; width: number; height: number };
/** Crop slide coordinates to the embedded image before drawing in its preview. */
export function relativeBox(box: Box, bounds: Box): Box | undefined {
  if (![box.x, box.y, box.width, box.height, bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
    || bounds.width <= 0 || bounds.height <= 0 || box.width <= 0 || box.height <= 0) return;
  const left = Math.max(box.x, bounds.x); const top = Math.max(box.y, bounds.y);
  const right = Math.min(box.x + box.width, bounds.x + bounds.width);
  const bottom = Math.min(box.y + box.height, bounds.y + bounds.height);
  if (right <= left || bottom <= top) return;
  return { x: (left - bounds.x) / bounds.width * 100, y: (top - bounds.y) / bounds.height * 100,
    width: (right - left) / bounds.width * 100, height: (bottom - top) / bounds.height * 100 };
}
