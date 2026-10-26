# Agent guidance

This is the standalone ComfyUI Audio Visualizer pack. Keep the registered
node identifier `AudioVisualizer` stable and migrate saved widget values
when changing input names or order. The renderer uses CPU FFmpeg libraries through PyAV, not GPU
inference or an FFmpeg executable. Do not add PyTorch or NumPy pins; ComfyUI provides them.

Use the bno84 Git identity and signed conventional commits. Commit bodies
explain the change, then `### Why` and relevant `### Testing` notes.
Append `Assisted-by: MODEL-ID` when creating or significantly changing commits.
Do not add redundant Co-Authored-By lines for agents.

Run `python3 -m unittest discover -s tests -v` and `node --test tests/*.mjs`.
Real rendering tests need PyAV and NumPy; they must run without FFmpeg or FFprobe executables.
Keep the selected-style capability checks and chunked feeding when changing the renderer.
Check widget migration, save/reload, conditional controls, and separator widths
when changing the frontend. Keep separators out of workflow and API values.

Publishing and remote pushes require user authorization. Local development
does not require or access Registry tokens.
