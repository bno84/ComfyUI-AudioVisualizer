import json
import os

import folder_paths

from . import render as R

try:
    from comfy_execution.graph_utils import ExecutionBlocker
except ImportError:  # older ComfyUI
    from comfy_execution.graph import ExecutionBlocker

CATEGORY = "audio/visualize"


class AudioVisualizer:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"lazy": True, "tooltip": "the still; sets the video size, odd sides +1px "
                                                             "(first image of a batch)"}),
                "audio": ("AUDIO", {"lazy": True}),
                "enabled": ("BOOLEAN", {"default": True, "tooltip": "off = render nothing and block the video "
                                                                    "output; image and audio aren't requested"}),
                "filename_prefix": ("STRING", {"default": "video/song_visualizer"}),
                "style": (list(R.STYLES), {"tooltip": "bars: 64 bars. dots: LED matrix. (none): still image and audio only"}),
                "dot_spacing": ("INT", {"default": 17, "min": 4, "max": 64, "display": "slider",
                                        "tooltip": "Distance between LED dots in pixels; also scales their diameter"}),
                "color": ("COLOR", {"default": "#9750c4", "tooltip": "bar color (#RRGGBB)"}),
                "band_height": ("FLOAT", {"default": R.DEFAULT_BAND, "min": R.BAND_RANGE[0], "max": R.BAND_RANGE[1],
                                          "step": 0.01, "display": "slider",
                                          "tooltip": "height of the spectrum band as a fraction of the video height"}),
                "gain": ("FLOAT", {"default": 1.3, "min": R.GAIN_RANGE[0], "max": R.GAIN_RANGE[1], "step": 0.05,
                                   "display": "slider",
                                   "tooltip": "height of the loudest moment, measured from the audio: 1.0 just "
                                              "reaches the top of the band, 1.3 clips the loudest peaks"}),
                "glow": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05, "display": "slider",
                                   "tooltip": "opacity of the blurred halo around bars; 0 disables it"}),
                "trail": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 0.99, "step": 0.01, "display": "slider",
                                    "tooltip": "per-frame brightness kept as bars fall; 0 disables it"}),
                "vignette": ("FLOAT", {"default": R.DEFAULT_VIGNETTE, "min": 0.0, "max": 1.0, "step": 0.05,
                                       "display": "slider",
                                       "tooltip": "edge darkening: 0 disables it, 0.4 matches the original, 1 is strongest"}),
                "fps": (list(R.FRAME_RATES), {"default": 60}),
                "start": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                    "tooltip": "seconds into the audio"}),
                "duration": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                       "tooltip": "seconds; 0 = to the end"}),
                "video_codec": (list(R.CODECS),),
                "h265_crf": ("INT", {"default": R.DEFAULT_CRF["h265"], "min": 0, "max": 51}),
                "h264_crf": ("INT", {"default": R.DEFAULT_CRF["h264"], "min": 0, "max": 51}),
                "audio_codec": (list(R.AUDIO_CODECS), {"default": "flac",
                                  "tooltip": "FLAC: lossless 24-bit. AAC / Opus: adjustable bitrate."}),
                "aac_bitrate": ("INT", {"default": 192, "min": 32, "max": 512,
                                      "tooltip": "AAC bitrate in kbit/s"}),
                "opus_bitrate": ("INT", {"default": 128, "min": 32, "max": 512,
                                        "tooltip": "Opus bitrate in kbit/s"}),
                "crf": ("INT", {"default": -1, "min": -1, "max": 51,
                                "tooltip": "Legacy workflow CRF; migrated to the selected codec control"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    OUTPUT_NODE = True
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = ("🎵 Still image + audio → 30 or 60 fps mp4 with a constant-Q spectrum band along the bottom, "
                   "rendered by ffmpeg in one pass. Saves to the output folder with the workflow embedded.")

    def check_lazy_status(self, enabled, image=None, audio=None, **_):
        return [name for name, value in (("image", image), ("audio", audio)) if enabled and value is None]

    def run(self, image, audio, enabled, filename_prefix, style, gain, color, glow, trail, crf, audio_codec,
            start, duration, prompt=None, extra_pnginfo=None, fps=60, aac_bitrate=192, h264_crf=22, h265_crf=18,
            video_codec="h265", opus_bitrate=128, dot_spacing=17, codec=None, audio_bitrate=None, dot=None,
            band_height=R.DEFAULT_BAND, vignette=R.DEFAULT_VIGNETTE):
        if not enabled:
            return {"ui": {}, "result": (ExecutionBlocker(None),)}
        import comfy.model_management as mm
        import comfy.utils
        from comfy.cli_args import args
        from comfy_api.latest import InputImpl

        import torch.nn.functional as F
        still = image[0, :, :, :3]
        height, width = still.shape[:2]
        if width % 2 or height % 2:                          # yuv420p needs even sizes: repeat the last row/column
            still = F.pad(still.permute(2, 0, 1)[None], (0, width % 2, 0, height % 2), mode="replicate")[0]
            still = still.permute(1, 2, 0)
            height, width = still.shape[:2]
        pixels = (still * 255).round().clamp(0, 255).byte().cpu().numpy().tobytes()
        waveform = audio["waveform"][0]                      # [channels, samples]
        samples = waveform.t().contiguous().float().cpu().numpy().astype("<f4").tobytes()

        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(
            f"{filename_prefix}_{style}", folder_paths.get_output_directory(), width, height)
        file = f"{name}_{counter:05}_.mp4"
        metadata = None
        if not args.disable_metadata:
            tags = {**(extra_pnginfo or {}), **({"prompt": prompt} if prompt is not None else {})}
            metadata = {key: json.dumps(value) for key, value in tags.items()} or None

        bar = None

        def on_frame(written, total):
            nonlocal bar
            mm.throw_exception_if_processing_interrupted()
            bar = bar or comfy.utils.ProgressBar(total)
            bar.update_absolute(written, total)

        path = os.path.join(folder, file)
        R.render(pixels, width, height, samples, audio["sample_rate"], waveform.shape[0], path,
                 style=style, color=color, dot=dot_spacing if dot is None else dot, gain=gain, glow=glow, trail=trail, codec=codec or video_codec, crf=crf,
                 audio_codec=audio_codec, audio_bitrate=opus_bitrate if audio_bitrate is None else audio_bitrate,
                 start=start, duration=duration,
                 fps=fps, aac_bitrate=aac_bitrate, h264_crf=h264_crf, h265_crf=h265_crf, metadata=metadata, on_frame=on_frame,
                 band_height=band_height, vignette=vignette)
        return {"ui": {"images": [{"filename": file, "subfolder": subfolder, "type": "output"}], "animated": (True,)},
                "result": (InputImpl.VideoFromFile(path),)}


NODE_CLASS_MAPPINGS = {"AudioVisualizer": AudioVisualizer}
NODE_DISPLAY_NAME_MAPPINGS = {"AudioVisualizer": "🎵 Audio visualizer video (still + spectrum)"}
