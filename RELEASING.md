# Releasing

The package starts at 0.1.0 with fresh Git history. No remote repository,
registry publication, or release tag is created by the local setup.

## One-time setup

1. Create `bno84/ComfyUI-AudioVisualizer` on GitHub and push `main`.
2. Confirm the `bno84` publisher owns the intended package ID
   `comfyui-audiovisualizer` before the first registry publication.
3. Add a Registry publishing token as the repository Actions secret
   `REGISTRY_ACCESS_TOKEN`. Never commit the token.

## Release

1. Update the semantic version in `pyproject.toml` and commit it.
2. Run the Python and frontend tests and `comfy node validate`.
3. Create a signed annotated tag `vX.Y.Z`; its message is the changelog.
4. Push the commit and tag. CI validates metadata, checks the tag/version
   match, runs tests including real FFmpeg renders, and loads the pack in
   ComfyUI 0.38.0 on CPU. Only then does the tag job publish to the registry.

Published versions are final. Fixes need a new version. Do not publish or
push a release tag merely to test the local setup.
