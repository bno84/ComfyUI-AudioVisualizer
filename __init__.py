"""Still image + audio → visualizer mp4 with a discrete spectrum band.

FFmpeg renders, encodes and muxes in one call;
frames never pass through the graph. The saved file embeds the workflow like ComfyUI's Save Video.
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
