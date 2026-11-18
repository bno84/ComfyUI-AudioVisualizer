import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const source = (await readFile(new URL('../web/preview.js', import.meta.url), 'utf8'))
  .replace('function draw(', 'export function draw(')
  .replace('class Preview {', 'export class Preview {')
  .replace('import { app } from "../../scripts/app.js";', 'const app = { registerExtension() {} };');
const { bandLayout, mockLevels, draw, vignetteFactor, spectrumPanels, splineSegments, PREVIEW_CONTROLS, Preview, activeSource } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

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
    setLineDash() {}, strokeRect() {}, rect() {}, clip() {}, beginPath() {}, fill() {},
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


test('stereo panels match renderer halves and odd-width center gaps for every style', () => {
  for (const style of ['bars', 'dots', 'spline']) {
    const options = { width: 640, height: 360, bandHeight: .35, dot: 17, style };
    for (const gap of [0, 80, 81, 200]) {
      const panels = spectrumPanels(options, true, gap);
      const half = Math.floor((640-gap)/2);
      assert.deepEqual(panels.map(p => [p.origin, p.direction]), [[0, 1], [640, -1]]);
      assert.deepEqual(panels[0].band, bandLayout({ ...options, width: half }));
      assert.deepEqual(panels[0].band, panels[1].band);
      const inner = panels[0].band.x + panels[0].band.w;
      assert.ok(640 - 2 * inner >= gap);
    }
    assert.equal(spectrumPanels(options, false, 81).length, 1);
    assert.deepEqual(spectrumPanels(options, true, 500), []);
  }
  assert.ok(PREVIEW_CONTROLS.includes('stereo_mirror'));
  assert.ok(PREVIEW_CONTROLS.includes('stereo_gap'));
});

test('mock draws two reflected spectra and switches spline to cubic curves', () => {
  for (const style of ['bars', 'dots', 'spline']) {
    const transforms = [], curves = [], bars = [], arcs = [];
    const context = {
      save() {}, restore() {}, clearRect() {}, drawImage() {},
      translate(...args) { transforms.push(['translate', ...args]); },
      scale(...args) { transforms.push(['scale', ...args]); },
      setLineDash() {}, strokeRect() {}, rect() {}, clip() {}, beginPath() {}, fill() {}, moveTo() {}, stroke() {},
      fillRect(...args) { bars.push(args); },
      bezierCurveTo(...args) { curves.push(args); },
      arc(...args) { arcs.push(args); },
      createRadialGradient() { return { addColorStop() {} }; },
    };
    const canvas = { width: 640, height: 360, getContext() { return context; } };
    const node = { widgets: Object.entries({ style, stereo_mirror: true, stereo_gap: 81 })
      .map(([name, value]) => ({ name, value })) };
    const result = draw(canvas, node, { naturalWidth: 640, naturalHeight: 360 });
    assert.equal(result.panels.length, 2);
    assert.ok(transforms.some(t => JSON.stringify(t) === JSON.stringify(['translate', 640, 0])));
    assert.ok(transforms.some(t => JSON.stringify(t) === JSON.stringify(['scale', -1, 1])));
    if (style === 'spline') {
      assert.equal(curves.length, 126);
      assert.equal(context.lineCap, 'round');
      assert.equal(context.lineJoin, 'round');
      assert.equal(bars.length, 0);
      assert.equal(arcs.length, 0);
      assert.deepEqual(curves.slice(0, 63), curves.slice(63));
    } else if (style === 'bars') assert.equal(bars.length, 128);
    else assert.ok(arcs.length > 0);
  }
});

test('spline mock uses bounded shape-preserving control points', () => {
  const segments = splineSegments([0, .5, 1, .5, 0], 401, 101);
  assert.deepEqual(segments[0].slice(0, 2), [1.5, 98.5]);
  assert.deepEqual(segments.at(-1).slice(-2), [398.5, 98.5]);
  assert.equal(segments[1][5], 1.5); // zero slope at the peak
  assert.equal(segments[2][3], 1.5);
  for (const segment of segments) {
    assert.ok(segment.every(Number.isFinite));
    assert.ok(segment.filter((_, i) => i % 2).every(y => y >= 0 && y <= 100));
  }
});


test('quiet and full-scale splines leave room for the entire stroke', () => {
  assert.ok(PREVIEW_CONTROLS.includes('line_thickness'));
  for (const thickness of [1, 2, 8, 32]) {
    for (const gain of [0, .001, .25, 1, 2]) {
      const segments = splineSegments(mockLevels(64, gain), 640, 126, thickness);
      for (const segment of segments) {
        for (let i = 0; i < segment.length; i += 2) {
          assert.ok(segment[i] - thickness/2 >= 0);
          assert.ok(segment[i] + thickness/2 <= 639);
          assert.ok(segment[i+1] - thickness/2 >= 0);
          assert.ok(segment[i+1] + thickness/2 <= 125);
        }
      }
    }
  }
});


test('silent spline mock remains a continuous bottomed-out line', () => {
  for (const thickness of [1, 2, 8, 32]) {
    const segments = splineSegments(Array(64).fill(0), 640, 126, thickness);
    const floor = 125 - thickness/2 - .5;
    for (const [i, segment] of segments.entries()) {
      assert.ok(segment.filter((_, index) => index % 2).every(y => y === floor));
      if (i) assert.deepEqual(segment.slice(0, 2), segments[i-1].slice(-2));
    }
  }
});


test('glow uses a fixed source-pixel blur and independent opacity before sharp stroke', () => {
  for (const width of [320, 640]) {
    for (const glow of [.2, .8]) {
      const passes = [], guides = [];
      const context = {
        save() {}, restore() {}, clearRect() {}, drawImage() {}, translate() {}, scale() {},
        beginPath() {}, rect() {}, clip() {}, moveTo() {}, bezierCurveTo() {},
        strokeRect() { guides.push(true); },
        stroke() { passes.push([this.filter, this.globalAlpha, this.lineWidth]); },
      };
      const canvas = { width, height: width/2, getContext() { return context; } };
      const node = { widgets: Object.entries({ style: 'spline', glow, line_thickness: 8 })
        .map(([name, value]) => ({ name, value })) };
      draw(canvas, node, { naturalWidth: 1280, naturalHeight: 640 });
      assert.deepEqual(passes, [[`blur(${5*width/1280}px)`, glow, 8], ['none', .9, 8]]);
      assert.deepEqual(guides, []);
    }
  }
});


const splineReference = JSON.parse(await readFile(new URL('./spline-reference.json', import.meta.url), 'utf8'));
test('mock spline matches renderer centerlines at clipped peaks and near-zero valleys', () => {
  for (const c of splineReference.cases) {
    const segments = splineSegments(c.heights.map(h => h/c.height), c.width, c.height, c.thickness);
    c.indices.forEach((index, n) => {
      const position = index/(2*c.width-1)*(c.heights.length-1);
      const i = Math.min(Math.floor(position), segments.length-1), t = position-i;
      const segment = segments[i];
      const cubic = axis => (1-t)**3*segment[axis] + 3*(1-t)**2*t*segment[axis+2]
        + 3*(1-t)*t*t*segment[axis+4] + t**3*segment[axis+6];
      assert.ok(Math.abs(cubic(0)-c.points[n][0]) < 1e-9);
      assert.ok(Math.abs(cubic(1)-c.points[n][1]) < 1e-9);
    });
  }
});


const dotsReference = JSON.parse(await readFile(new URL('./dots-reference.json', import.meta.url), 'utf8'));
test('mock dot diameter and pitch agree with actual FFmpeg grid samples, including stereo halves', () => {
  for (const c of dotsReference.cases) {
    const band = bandLayout({ width: c.width, height: c.height*2, bandHeight: .5, style: 'dots', dot: c.dot });
    assert.equal(band.w, c.actual_width);
    assert.equal(band.columns, c.columns);
    assert.equal(band.rows, c.rows);
    for (const [x, y, expected] of c.samples) {
      // Canvas gradients are centered on pixel edges; FFmpeg samples pixel centers.
      const cx = (Math.floor((x+.5)/band.cell)+.5)*band.cell;
      const cy = (Math.floor((y+.5)/band.rowHeight)+.5)*band.rowHeight;
      const alpha = Math.max(0, Math.min(1, band.radius-Math.hypot(x+.5-cx, y+.5-cy)));
      assert.ok(Math.abs(alpha*255-expected) <= 1.01,
        JSON.stringify({ width: c.width, dot: c.dot, x, y, expected, actual: alpha*255 }));
    }
  }
});


test('preview rerenders at higher resolution when zoomed without changing node layout', () => {
  const previousRatio = globalThis.devicePixelRatio;
  globalThis.devicePixelRatio = 1;
  try {
    let displayedWidth = 320, draws = 0;
    const image = { complete: true, naturalWidth: 1280, naturalHeight: 720, src: 'still.png' };
    const preview = Object.create(Preview.prototype);
    preview.node = {
      inputs: [{ name: 'image' }], getInputNode() { return { imgs: [image] }; },
      widgets: [{ name: 'style', value: '(none)' }],
      setSize() { assert.fail('resolution changes must not resize the node'); },
    };
    preview.aspect = 720/1280;
    preview.canvas = {
      clientWidth: 320,
      getBoundingClientRect() { return { width: displayedWidth }; },
      getContext() { return {
        save() {}, restore() {}, clearRect() {}, drawImage() { draws++; },
      }; },
    };
    preview.update();
    assert.equal(preview.canvas.width, 640);
    assert.equal(preview.canvas.height, 360);
    preview.update();
    assert.equal(draws, 1);
    displayedWidth = 960; // 3x ComfyUI zoom, same DOM layout width.
    preview.update();
    assert.equal(preview.canvas.width, 1920);
    assert.equal(preview.canvas.height, 1080);
    assert.equal(draws, 2);
    displayedWidth = 160; // Preserve supersampling while zoomed out.
    globalThis.devicePixelRatio = 2;
    preview.update();
    assert.equal(preview.canvas.width, 1280);
    displayedWidth = 10000;
    preview.update();
    assert.equal(preview.canvas.width, 4096);
  } finally {
    globalThis.devicePixelRatio = previousRatio;
  }
});


test('bar mock uses the selected count for each stereo half and keeps other styles independent', () => {
  assert.ok(PREVIEW_CONTROLS.includes('bar_count'));
  assert.equal(bandLayout({ width: 320, height: 180, bandHeight: .35, dot: 17, style: 'bars', barCount: 256 }), null);
  for (const count of [8, 32, 64, 128, 256]) {
    const options = { width: 1028, height: 360, bandHeight: .35, dot: 17, style: 'bars', barCount: count };
    const panels = spectrumPanels(options, true, 0);
    for (const { band } of panels) {
      assert.equal(band.columns, count);
      assert.equal(band.cell, band.w/count);
      assert.ok(band.cell > band.x);
      const expectedMargin = 514 >= count*7 ? 6 : Math.max(1, Math.floor(514/(count*3)));
      assert.equal(band.x, expectedMargin);
    }
    assert.equal(bandLayout({ ...options, style: 'spline' }).columns, 64);
    assert.equal(bandLayout({ ...options, style: 'dots' }).columns, Math.floor(1028/17));
  }
});

test('a bypassed, muted or missing image source leaves the default still to the render', () => {
  const node = (source) => ({ inputs: [{ name: 'audio' }, { name: 'image' }], getInputNode: (slot) => (slot === 1 ? source : null) });
  const loader = (mode) => ({ mode, imgs: [] });
  assert.equal(activeSource(node(loader(0))).mode, 0);
  for (const mode of [2, 4]) assert.equal(activeSource(node(loader(mode))), null);
  assert.equal(activeSource(node(null)), null);
  assert.equal(activeSource({ inputs: [] }), null);
});
