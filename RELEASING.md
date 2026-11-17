# Releasing

Releases are tag-driven. Main pushes run CI without publishing; a pushed
`v*` tag publishes to the Comfy Registry only after the required checks pass.

## One-time setup

1. Create `bno84/ComfyUI-AudioVisualizer` on GitHub and push `main`.
2. Confirm the `bno84` publisher owns the intended package ID
   `comfyui-audiovisualizer` before the first registry publication.
3. Add a Registry publishing token as the repository Actions secret
   `REGISTRY_ACCESS_TOKEN`. Never commit the token.

## Release

1. Update the semantic version in `pyproject.toml` and commit it.
2. Run the Python and frontend tests and `comfy node validate`.
3. Push `main`: `git push origin main`. Wait for CI to pass on that exact
   commit before tagging. If another commit is added, wait for its CI too.
   The required jobs are metadata/security/frontend, Linux and Windows
   rendering, and the ComfyUI CPU smoke test. Optional macOS jobs do not
   block publishing.
4. Create a signed annotated tag `vX.Y.Z` on the tested commit; its message
   is the changelog.
5. Push only the tag: `git push origin vX.Y.Z`. The tag workflow repeats
   validation, checks the tag/version match, runs real FFmpeg renders, and
   loads the pack in ComfyUI 0.38.0 on CPU before publishing to the registry.

Published versions are final. Fixes need a new version. Do not publish or
push a release tag merely to test the local setup.
