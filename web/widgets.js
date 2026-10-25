import { app } from "../../scripts/app.js";

// Keep hidden widgets in the array: workflows serialize their values by position.
const originals = new WeakMap();
const defaults = new WeakMap();
const ORDER_KEY = "sdcpp_audio_visualizer_widget_order";
// Before band_height existed, gain was showcqt's raw volume; it now scales the measured loudest moment.
const MEASURED_GAIN_SINCE = "band_height";
const LEGACY_ORDER = [
  "enabled", "filename_prefix", "style", "gain", "color", "dot", "glow", "trail",
  "codec", "crf", "audio_codec", "audio_bitrate", "start", "duration", "fps",
  "aac_bitrate", "h264_crf", "h265_crf",
];

function addSeparator(node, before) {
  const separator = {
    name: `__separator_${before}`,
    type: "custom",
    serialize: false,
    options: { serialize: false },
    computeSize: () => [0, 12],
    draw(ctx, node, _width, y) {
      ctx.save();
      ctx.strokeStyle = "#888";
      ctx.globalAlpha = 0.4;
      ctx.beginPath();
      ctx.moveTo(12, y + 6);
      ctx.lineTo(Math.max(12, node.size[0] - 12), y + 6);
      ctx.stroke();
      ctx.restore();
    },
  };
  node.addCustomWidget(separator);
  node.widgets.splice(node.widgets.indexOf(separator), 1);
  node.widgets.splice(node.widgets.findIndex((w) => w.name === before), 0, separator);
}

export function updateWidgets(node) {
  const value = (name) => node.widgets?.find((w) => w.name === name)?.value;
  let changed = false;
  for (const [name, visible] of [
    ["dot_spacing", value("style") === "dots"],
    ["crf", false],
    ["h264_crf", value("video_codec") === "h264"],
    ["h265_crf", value("video_codec") === "h265"],
    ["opus_bitrate", value("audio_codec") === "opus"],
    ["aac_bitrate", value("audio_codec") === "aac"],
  ]) {
    const widget = node.widgets?.find((w) => w.name === name);
    if (!widget) continue;
    if (!visible && !originals.has(widget)) {
      originals.set(widget, { type: widget.type, computeSize: widget.computeSize, hidden: widget.hidden });
      widget.type = "hidden";
      widget.hidden = true;
      widget.computeSize = () => [0, -4];
      changed = true;
    } else if (visible && originals.has(widget)) {
      Object.assign(widget, originals.get(widget));
      originals.delete(widget);
      changed = true;
    }
  }
  if (changed) {
    node.setSize([node.size[0], node.computeSize()[1]]);
    node.graph?.setDirtyCanvas(true, true);
  }
}

app.registerExtension({
  name: "bno84.AudioVisualizerWidgets",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "AudioVisualizer") return;
    const onSerialize = nodeType.prototype.onSerialize;
    nodeType.prototype.onSerialize = function (data) {
      const result = onSerialize?.call(this, data);
      data.properties ??= {};
      data.properties[ORDER_KEY] = this.widgets.filter((w) => w.serialize !== false).map((w) => w.name);
      return result;
    };
    for (const hook of ["onNodeCreated", "onConfigure", "onWidgetChanged"]) {
      const previous = nodeType.prototype[hook];
      nodeType.prototype[hook] = function (...args) {
        const result = previous?.apply(this, args);
        if (hook === "onNodeCreated") {
          defaults.set(this, Object.fromEntries(this.widgets.map((w) => [w.name, w.value])));
          addSeparator(this, "fps");
          addSeparator(this, "video_codec");
        }
        if (hook === "onConfigure") {
          const data = args[0];
          const order = data.properties?.[ORDER_KEY] ?? LEGACY_ORDER;
          const values = { ...defaults.get(this) };
          data.widgets_values?.forEach((value, index) => {
            const name = order[index];
            const renamed = { codec: "video_codec", audio_bitrate: "opus_bitrate", dot: "dot_spacing" };
            if (name) values[renamed[name] ?? name] = value;
          });
          if (data.widgets_values?.length && !order.includes(MEASURED_GAIN_SINCE)) {
            if (values.gain !== defaults.get(this).gain) {
              console.info(`Audio visualizer: gain ${values.gain} was a raw volume; now ${defaults.get(this).gain} scales the measured loudest moment.`);
            }
            values.gain = defaults.get(this).gain;
          }
          for (const widget of this.widgets) {
            if (Object.hasOwn(values, widget.name)) widget.value = values[widget.name];
          }
          const legacy = this.widgets?.find((w) => w.name === "crf");
          const codec = this.widgets?.find((w) => w.name === "video_codec")?.value;
          const quality = this.widgets?.find((w) => w.name === `${codec}_crf`);
          if (legacy?.value >= 0 && quality) {
            quality.value = legacy.value;
            legacy.value = -1;
          }
        }
        updateWidgets(this);
        return result;
      };
    }
  },
});
