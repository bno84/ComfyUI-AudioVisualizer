import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const source = (await readFile(new URL('../web/preview.js', import.meta.url), 'utf8'))
  .replace('function draw(', 'export function draw(')
  .replace('import { app } from "../../scripts/app.js";', 'const app = { registerExtension() {} };');
const { bandLayout, mockLevels, draw } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('the band sits where render.py puts it', () => {
  assert.deepEqual(bandLayout({ width: 1280, height: 720, bandHeight: 0.16, style: 'bars', dot: 12 }),
    { x: 6, y: 720 - 116 - 3, w: 1274, h: 116, columns: 64, rows: 0, cell: 1274 / 64 });
  assert.deepEqual(bandLayout({ width: 1280, height: 720, bandHeight: 0.2, style: 'dots', dot: 12 }),
    { x: 0, y: 720 - 144 - 3, w: 1280, h: 144, columns: 106, rows: 12, cell: 1280 / 106, rowHeight: 12, radius: 12 * 0.4 });
});

test('dots preview centers complete circles in the stretched mask cells', () => {
  const arcs = [];
  const canvas = { width: 572, height: 778, getContext() { return {
    save() {}, restore() {}, clearRect() {}, drawImage() {}, translate() {}, scale() {},
    setLineDash() {}, strokeRect() {}, beginPath() {}, fill() {},
    arc(...args) { arcs.push(args); },
  }; } };
  const node = { widgets: [
    { name: 'style', value: 'dots' }, { name: 'dot_spacing', value: 17 },
    { name: 'band_height', value: .35 },
  ] };
  const { band } = draw(canvas, node, { naturalWidth: 572, naturalHeight: 778 });
  assert.ok(arcs.length > 0);
  for (const [x, y, radius] of arcs) {
    const column = Math.floor((x + .5) / band.cell);
    const row = Math.floor((y + .5) / band.rowHeight);
    assert.ok(Math.abs(x - ((column + .5) * band.cell - .5)) < 1e-10);
    assert.ok(Math.abs(y - ((row + .5) * band.rowHeight - .5)) < 1e-10);
    assert.ok(x - radius >= column * band.cell - .5);
    assert.ok(x + radius <= (column + 1) * band.cell - .5);
    assert.ok(y - radius >= row * band.rowHeight - .5);
    assert.ok(y + radius <= (row + 1) * band.rowHeight - .5);
  }
});

test('the mock spectrum peaks at exactly the gain', () => {
  for (const gain of [1, 1.15, 0.5]) {
    const levels = mockLevels(64, gain);
    assert.equal(levels.length, 64);
    assert.ok(Math.abs(Math.max(...levels) - gain) < 1e-12);
    assert.ok(levels.every(level => level >= 0));
  }
  // Above 1 only the loudest note clips; most of the band stays well below the top, as with real audio.
  const levels = mockLevels(64, 1.15);
  const clipped = levels.filter(level => level > 1).length;
  assert.ok(clipped >= 1 && clipped <= 3, `${clipped} clipped columns`);
  assert.ok(levels.filter(level => level < 0.5).length > 48);
});

test('none preview draws the still without a band or spectrum', () => {
  assert.equal(bandLayout({ width: 640, height: 144, bandHeight: 0.35, style: '(none)', dot: 17 }), null);
  let drawn = false, restored = false;
  const canvas = { width: 640, height: 144, getContext() {
    return { save() {}, clearRect() {}, drawImage() { drawn = true; }, restore() { restored = true; } };
  }};
  const node = { widgets: [{ name: 'style', value: '(none)' }] };
  const result = draw(canvas, node, { naturalWidth: 640, naturalHeight: 144 });
  assert.equal(result.band, null);
  assert.ok(drawn && restored);
});
