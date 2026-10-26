import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const source = (await readFile(new URL('../web/preview.js', import.meta.url), 'utf8'))
  .replace('function draw(', 'export function draw(')
  .replace('import { app } from "../../scripts/app.js";', 'const app = { registerExtension() {} };');
const { bandLayout, mockLevels, draw, vignetteFactor } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('the band sits where render.py puts it', () => {
  assert.deepEqual(bandLayout({ width: 1280, height: 720, bandHeight: 0.16, style: 'bars', dot: 12 }),
    { x: 6, y: 720 - 116 - 3, w: 1274, h: 116, columns: 64, rows: 0, cell: 1274 / 64 });
  assert.deepEqual(bandLayout({ width: 1280, height: 720, bandHeight: 0.2, style: 'dots', dot: 12 }),
    { x: 0, y: 720 - 144 - 3, w: 1280, h: 144, columns: 106, rows: 12, cell: 1280 / 106, rowHeight: 12, radius: 12 * 0.4 });
});

test('dots preview centers complete circles in the stretched mask cells', () => {
  const arcs = [], gradients = [];
  const canvas = { width: 572, height: 778, getContext() { return {
    save() {}, restore() {}, clearRect() {}, drawImage() {}, translate() {}, scale() {},
    setLineDash() {}, strokeRect() {}, beginPath() {}, fill() {},
    arc(...args) { arcs.push(args); },
    createRadialGradient(...args) {
      const stops = [];
      gradients.push({ args, stops });
      return { addColorStop(...stop) { stops.push(stop); } };
    },
  }; } };
  const node = { widgets: [
    { name: 'style', value: 'dots' }, { name: 'dot_spacing', value: 17 },
    { name: 'band_height', value: .35 },
  ] };
  const { band } = draw(canvas, node, { naturalWidth: 572, naturalHeight: 778 });
  assert.ok(arcs.length > 0);
  assert.equal(gradients.length, arcs.length);
  for (const [i, [x, y, radius]] of arcs.entries()) {
    const column = Math.floor(x / band.cell);
    const row = Math.floor(y / band.rowHeight);
    assert.ok(Math.abs(x - ((column + .5) * band.cell)) < 1e-10);
    assert.ok(Math.abs(y - ((row + .5) * band.rowHeight)) < 1e-10);
    assert.ok(x - radius >= column * band.cell);
    assert.ok(x + radius <= (column + 1) * band.cell);
    assert.ok(y - radius >= row * band.rowHeight);
    assert.ok(y + radius <= (row + 1) * band.rowHeight);
    assert.deepEqual(gradients[i], {
      args: [x, y, radius - 1, x, y, radius],
      stops: [[0, '#9750c4'], [1, 'transparent']],
    });
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

const vignetteReference = JSON.parse(await readFile(new URL('./vignette-reference.json', import.meta.url), 'utf8'));

test('vignette agrees with actual FFmpeg RGB filter samples at every tested strength/aspect', () => {
  for (const { width, height, strength, points } of vignetteReference.cases) {
    for (const { x, y, value } of points) {
      const actual = 200 * vignetteFactor(x, y, width, height, strength);
      assert.ok(Math.abs(actual - value) <= 1.01,
        JSON.stringify({ width, height, strength, x, y, expected: value, actual }));
    }
    assert.equal(vignetteFactor(0, 0, width, height, 0), 1);
  }
});

test('vignette mask caches source dimensions, preview resolution and strength', () => {
  const previousDocument = globalThis.document;
  const masks = [], drawn = [];
  globalThis.document = { createElement(name) {
    assert.equal(name, 'canvas');
    const mask = { getContext() { return {
      createImageData(w, h) { return { data: new Uint8ClampedArray(w*h*4) }; },
      putImageData(image) { mask.rgba = image.data; },
    }; } };
    masks.push(mask);
    return mask;
  } };
  try {
    const canvas = { width: 120, height: 160, getContext() { return {
      save() {}, restore() {}, clearRect() {}, drawImage(image) { drawn.push(image); },
    }; } };
    const node = { widgets: [{ name: 'style', value: '(none)' }, { name: 'vignette', value: 1 }] };
    const still = { naturalWidth: 720, naturalHeight: 960 };
    draw(canvas, node, still);
    assert.equal(masks.length, 1);
    assert.equal(masks[0].rgba[3], 255);
    assert.equal(masks[0].rgba[(80*120+60)*4+3], 0);
    assert.deepEqual(drawn, [still, masks[0]]);
    draw(canvas, node, still);
    assert.equal(masks.length, 1);
    node.widgets[1].value = .4;
    draw(canvas, node, still);
    assert.equal(masks.length, 2);
    canvas.width = 180;
    draw(canvas, node, still);
    assert.equal(masks.length, 3);
    draw(canvas, node, { naturalWidth: 960, naturalHeight: 720 });
    assert.equal(masks.length, 4);
    node.widgets[1].value = 0;
    drawn.length = 0;
    draw(canvas, node, still);
    assert.deepEqual(drawn, [still]);
  } finally {
    globalThis.document = previousDocument;
  }
});
