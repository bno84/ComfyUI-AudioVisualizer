"""Metadata embedded in the mp4: the workflow and prompt, plus optional player info.

The mp4 is written with free-form keys (FFmpeg's use_metadata_tags), the only
way to keep the workflow and prompt. Players that read those keys, such as
Dragon Player, show the title; others, such as VLC and TagLib-based ones, do not.
"""

import json

PLAYER_INFO = ("title", "artist", "comment", "lyrics")


def video_metadata(extra_pnginfo=None, prompt=None, **player_info):
    """JSON-encoded workflow and prompt, plus each non-empty player info value as plain text; None if empty."""
    tags = {**(extra_pnginfo or {}), **({"prompt": prompt} if prompt is not None else {})}
    metadata = {key: json.dumps(value) for key, value in tags.items()}
    for key in PLAYER_INFO:
        value = player_info.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    return metadata or None
