import { app } from "../../scripts/app.js";

// A mock of the video's first seconds: the connected still with the render's
// vignette and band layout, and a made-up spectrum whose loudest bar is `gain`
// times the band height. It checks placement, not the audio.
const NODE = "AudioVisualizer";
const MARGIN = 6;          // render.py: gap between bars
const BOTTOM_MARGIN = 3;   // render.py: gap below the band
const PLACEHOLDER = [1280, 720];

// The band in image pixels, as render.py lays it out.
export function bandLayout({ width, height, bandHeight, style, dot }) {
  const h = 2 * Math.round((height * bandHeight) / 2);   // showcqt needs an even height
  const y = height - h - BOTTOM_MARGIN;
  if (style === "dots") {
    return { x: 0, y, w: width, h, columns: Math.floor(width / dot), rows: Math.floor(h / dot), cell: dot };
  }
  return { x: MARGIN, y, w: width - MARGIN, h, columns: 64, rows: 0, cell: (width - MARGIN) / 64 };
}

// A typical moment of a song at its loudest: one narrow note at full height
// over a bass bump near 60%, mids near 30% and a low treble tail.
// Normalized so the loudest column is exactly `gain`; above 1 it clips.
export function mockLevels(columns, gain) {
  const levels = Array.from({ length: columns }, (_, i) => {
    const f = columns > 1 ? i / (columns - 1) : 0;
    const bump = (center, width, size) => size * Math.exp(-(((f - center) / width) ** 2));
    return 0.05 + bump(0.03, 0.05, 0.6) + bump(0.4, 0.2, 0.25) + bump(0.15, 0.015, 0.95)
      + 0.06 * Math.sin(i * 1.7) ** 2 - 0.08 * f;
  });
  const loudest = Math.max(...levels);
  return levels.map((level) => (gain * Math.max(level, 0)) / loudest);
}

function stillOf(node) {
  const slot = node.inputs?.findIndex((input) => input.name === "image");
  const source = slot >= 0 ? node.getInputNode?.(slot) : null;
  const image = source?.imgs?.[0];
  return image?.complete && image.naturalWidth ? image : null;
}

function value(node, name) {
  return node.widgets?.find((w) => w.name === name)?.value;
}

function draw(canvas, node, still) {
  const [width, height] = still ? [still.naturalWidth, still.naturalHeight] : PLACEHOLDER;
  const style = value(node, "style") ?? "bars";
  const band = bandLayout({
    width, height, style, bandHeight: value(node, "band_height") ?? 0.35, dot: value(node, "dot_spacing") ?? 17,
  });
  const scale = canvas.width / width;
  const g = canvas.getContext("2d");
  g.save();
  g.clearRect(0, 0, canvas.width, canvas.height);
  if (still) {
    g.drawImage(still, 0, 0, canvas.width, canvas.height);
  } else {
    g.fillStyle = "#222";
    g.fillRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = "#888";
    g.font = `${Math.round(14 * devicePixelRatio)}px sans-serif`;
    g.textAlign = "center";
    g.fillText("Connect a Load Image node to preview your still", canvas.width / 2, canvas.height / 3);
  }
  // Approximate FFmpeg's edge darkening; zero leaves the still untouched.
  const strength = value(node, "vignette") ?? 0.4;
  if (strength > 0) {
    const cx = canvas.width / 2, cy = canvas.height / 2, r = Math.hypot(cx, cy);
    const vignette = g.createRadialGradient(cx, cy, r * 0.45, cx, cy, r);
    vignette.addColorStop(0, "rgba(0,0,0,0)");
    vignette.addColorStop(1, `rgba(0,0,0,${Math.min(1, 0.45 * strength / 0.4)})`);
    g.fillStyle = vignette;
    g.fillRect(0, 0, canvas.width, canvas.height);
  }

  g.translate(band.x * scale, band.y * scale);
  g.scale(scale, scale);
  g.setLineDash([6 / scale, 6 / scale]);
  g.strokeStyle = "rgba(255,255,255,0.35)";
  g.lineWidth = 1 / scale;
  g.strokeRect(0, 0, band.w, band.h);
  g.setLineDash([]);

  const color = value(node, "color") ?? "#9750c4";
  const glow = value(node, "glow") ?? 0;
  g.fillStyle = color;
  g.globalAlpha = 0.9;
  if (glow > 0) {
    g.shadowColor = color;
    g.shadowBlur = 10 * glow * scale;
  }
  const levels = mockLevels(band.columns, value(node, "gain") ?? 1.3);
  levels.forEach((level, column) => {
    const shown = Math.min(level, 1);
    if (style === "dots") {
      const lit = Math.round(shown * band.rows);
      for (let row = 0; row < lit; row++) {
        g.beginPath();
        g.arc((column + 0.5) * band.cell, band.h - (row + 0.5) * band.cell, band.cell * 0.4, 0, 2 * Math.PI);
        g.fill();
      }
    } else {
      const top = band.h * (1 - shown);
      g.fillRect(column * band.cell, top, band.cell - MARGIN, band.h - top);
    }
  });
  g.restore();
  return { width, height, band };
}

class Preview {
  constructor(node) {
    this.node = node;
    this.root = document.createElement("div");
    this.root.style.cssText = "width:100%;display:flex;flex-direction:column;gap:2px;";
    this.canvas = document.createElement("canvas");
    this.canvas.style.cssText = "width:100%;border-radius:4px;";
    this.caption = document.createElement("div");
    this.caption.style.cssText = "font:11px sans-serif;opacity:.7;text-align:center;";
    this.root.append(this.canvas, this.caption);
    this.aspect = PLACEHOLDER[1] / PLACEHOLDER[0];
    this.widget = node.addDOMWidget("preview", "preview", this.root, {
      serialize: false, canvasOnly: true, hideOnZoom: false,
      getMinHeight: () => Math.max(80, (this.node.size[0] - 20) * this.aspect + 18),
    });
    this.widget.serialize = false;
  }

  // Cheap enough to call on every canvas draw; repaints only when something changed.
  update() {
    const still = stillOf(this.node);
    const names = ["style", "band_height", "dot_spacing", "color", "glow", "gain", "vignette"];
    const width = Math.round(this.canvas.clientWidth * devicePixelRatio);
    const key = JSON.stringify([still?.src, width, names.map((n) => value(this.node, n))]);
    if (key === this.key || !width) return;
    this.key = key;
    const aspect = still ? still.naturalHeight / still.naturalWidth : PLACEHOLDER[1] / PLACEHOLDER[0];
    this.canvas.width = width;
    this.canvas.height = Math.round(width * aspect);
    const { height, band } = draw(this.canvas, this.node, still);
    const gain = value(this.node, "gain") ?? 1.3;
    this.caption.textContent = `Mock spectrum · band ${band.h}px of ${height}px · loudest moment at ${Math.round(gain * 100)}% of the band`;
    if (aspect !== this.aspect) {
      this.aspect = aspect;
      this.node.setSize([this.node.size[0], this.node.computeSize()[1]]);
    }
  }
}

app.registerExtension({
  name: "bno84.AudioVisualizerPreview",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;
    const onDrawForeground = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (...args) {
      const result = onDrawForeground?.apply(this, args);
      this.audioVisualizerPreview?.update();
      return result;
    };
  },
  nodeCreated(node) {
    if (node.comfyClass !== NODE) return;
    node.audioVisualizerPreview = new Preview(node);
  },
});
