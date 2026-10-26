import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import assert from 'node:assert/strict';

let extension;
globalThis.__audioVisualizerTestApp = { registerExtension(value) { extension = value; } };
const source = (await readFile(new URL('../web/widgets.js', import.meta.url), 'utf8'))
  .replace('import { app } from "../../scripts/app.js";', 'const app = globalThis.__audioVisualizerTestApp;');
await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
delete globalThis.__audioVisualizerTestApp;

const initial = {
  enabled: true, filename_prefix: 'video/song_visualizer', style: 'bars', dot_spacing: 17,
  color: '#9750c4', band_height: 0.35, gain: 1.3, glow: 0, trail: 0, vignette: 0, fps: 60, start: 0, duration: 0,
  video_codec: 'h265', h265_crf: 18, h264_crf: 22, audio_codec: 'flac',
  aac_bitrate: 192, opus_bitrate: 128, crf: -1,
};
class Node {
  constructor() {
    this.widgets = Object.entries(initial).map(([name, value]) => ({ name, value, type: 'test' }));
    this.size = [400, 500];
    this.onNodeCreated();
  }
  addCustomWidget(widget) { this.widgets.push(widget); }
  computeSize() { return [400, 500]; }
  setSize(size) { this.size = size; }
  configure(data) { this.onConfigure(data); }
  serialize() {
    const data = { properties: {}, widgets_values: this.widgets.filter(w => w.serialize !== false).map(w => w.value) };
    this.onSerialize(data);
    return data;
  }
}
extension.beforeRegisterNodeDef(Node, { name: 'AudioVisualizer' });
const widget = (node, name) => node.widgets.find(w => w.name === name);
const values = node => Object.fromEntries(node.widgets.filter(w => w.serialize !== false).map(w => [w.name, w.value]));
const change = (node, name, value) => {
  widget(node, name).value = value;
  node.onWidgetChanged(name, value);
};

test('three unlabeled groups, with dot spacing and codec controls beside their selector', () => {
  const node = new Node();
  assert.deepEqual(node.widgets.filter(w => !w.hidden).map(w => w.name), [
    'enabled', 'filename_prefix', 'style', 'color', 'band_height', 'gain', 'glow', 'trail', 'vignette',
    '__separator_fps', 'fps', 'start', 'duration', '__separator_video_codec',
    'video_codec', 'h265_crf', 'audio_codec',
  ]);
  change(node, 'style', 'dots');
  assert.equal(widget(node, 'dot_spacing').hidden, undefined);
  assert.equal(node.widgets[node.widgets.indexOf(widget(node, 'style')) + 1].name, 'dot_spacing');
  change(node, 'audio_codec', 'aac');
  assert.equal(widget(node, 'aac_bitrate').value, 192);
  assert.equal(widget(node, 'aac_bitrate').hidden, undefined);
  change(node, 'aac_bitrate', 256);
  change(node, 'audio_codec', 'opus');
  assert.equal(widget(node, 'aac_bitrate').hidden, true);
  assert.equal(widget(node, 'opus_bitrate').value, 128);
  change(node, 'audio_codec', 'aac');
  assert.equal(widget(node, 'aac_bitrate').value, 256);
});

test('original positional workflows migrate values and explicit CRF, including zero', () => {
  const node = new Node();
  node.configure({ widgets_values: [false, 'audio/old', 'dots', 12, '#9750c4', 24, 0.2, 0.3,
    'h264', 0, 'opus', 160, 2, 8] });
  assert.deepEqual(values(node), { ...initial, enabled: false, filename_prefix: 'audio/old',
    style: 'dots', color: '#9750c4', dot_spacing: 24, glow: 0.2, trail: 0.3,
    video_codec: 'h264', h264_crf: 0, audio_codec: 'opus', opus_bitrate: 160, start: 2, duration: 8 });
  const copy = new Node();
  copy.configure(node.serialize());
  assert.deepEqual(values(copy), values(node));
});

test('workflows from before band_height reset gain, which meant a raw volume', () => {
  const node = new Node();
  const order = Object.keys(initial).filter(name => name !== 'band_height');
  node.configure({ properties: { sdcpp_audio_visualizer_widget_order: order },
    widgets_values: order.map(name => (name === 'gain' ? 12 : initial[name])) });
  assert.equal(widget(node, 'gain').value, 1.3);
  change(node, 'gain', 0.9);
  const copy = new Node();
  copy.configure(node.serialize());
  assert.equal(widget(copy, 'gain').value, 0.9);
  assert.equal(widget(copy, 'band_height').value, 0.35);
});

test('named widget orders survive reordering, renamed controls, and save/reload', () => {
  const node = new Node();
  node.configure({ properties: { sdcpp_audio_visualizer_widget_order: ['codec', 'dot', 'audio_bitrate', 'fps'] },
    widgets_values: ['h264', 32, 256, 30] });
  assert.equal(widget(node, 'video_codec').value, 'h264');
  assert.equal(widget(node, 'dot_spacing').value, 32);
  assert.equal(widget(node, 'opus_bitrate').value, 256);
  assert.equal(widget(node, 'fps').value, 30);
  const saved = node.serialize();
  assert.equal(saved.widgets_values.length, Object.keys(initial).length);
  assert.equal(saved.properties.sdcpp_audio_visualizer_widget_order.some(n => n.startsWith('__separator')), false);
  const copy = new Node();
  copy.configure(saved);
  assert.deepEqual(values(copy), values(node));
});

test('separators follow current width and ignore a stale widget width', () => {
  const node = new Node();
  for (const width of [400, 700, 450]) {
    node.size[0] = width;
    for (const separator of node.widgets.filter(w => w.serialize === false)) {
      const points = [];
      separator.draw({ save() {}, restore() {}, beginPath() {}, stroke() {},
        moveTo(x, y) { points.push([x, y]); }, lineTo(x, y) { points.push([x, y]); } }, node, 500, 20);
      assert.deepEqual(points, [[12, 26], [width - 12, 26]]);
      assert.equal(separator.options.serialize, false);
    }
  }
});

test('workflows without vignette default to off; explicit strength survives save/reload', () => {
  const node = new Node();
  const order = Object.keys(initial).filter(name => name !== 'vignette');
  node.configure({ properties: { sdcpp_audio_visualizer_widget_order: order },
    widgets_values: order.map(name => initial[name]) });
  assert.equal(widget(node, 'vignette').value, 0);
  for (const strength of [0, 0.2, 0.4, 1]) {
    change(node, 'vignette', strength);
    const copy = new Node();
    copy.configure(node.serialize());
    assert.equal(widget(copy, 'vignette').value, strength);
    assert.equal(widget(copy, 'fps').value, 60);
  }
});

test('none hides spectrum controls and preserves settings across switching and reload', () => {
  const node = new Node();
  change(node, 'style', '(none)');
  for (const name of ['color', 'band_height', 'gain', 'glow', 'trail', 'dot_spacing']) {
    assert.equal(widget(node, name).hidden, true);
  }
  for (const name of ['vignette']) {
    assert.ok(!widget(node, name).hidden);
  }
  const copy = new Node();
  copy.configure(node.serialize());
  assert.deepEqual(values(copy), values(node));
  change(copy, 'style', 'bars');
  assert.ok(!widget(copy, 'color').hidden);
  assert.equal(widget(copy, 'gain').value, 1.3);
});

test('saved none style migrates to (none)', () => {
  const node = new Node();
  const order = Object.keys(initial);
  node.configure({ properties: { sdcpp_audio_visualizer_widget_order: order },
    widgets_values: order.map(name => name === 'style' ? 'none' : initial[name]) });
  assert.equal(widget(node, 'style').value, '(none)');
  assert.equal(widget(node, 'gain').hidden, true);
});
