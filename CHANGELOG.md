# Changelog

## Unreleased (since `6040fd0`)

### Added
- **Real-time media auto-download**: When a new media message arrives via the webhook, the app now polls wacli's own media directory (`~/.wacli/media/<jid>/<msg_id>/`) where `--download-media` stores files, copies them to `wa_media/`, registers in `meta_wa.json`, generates thumbnails, and broadcasts a `media_stored` SSE event — all within seconds instead of waiting for the 5-minute background loop. (#2)

### Changed
- **Targeted card updates**: `media_stored` SSE now calls `updateCardStored(msgId)` which swaps just one card's `outerHTML` instead of re-rendering the entire grid.
- **Non-previewable file types**: Added `has_preview` field to API. Files without previews (ZIP, audio, etc.) show a large file-type icon instead of a broken thumbnail, and clicking downloads directly.
- **`renderCardHtml()` extracted**: Card HTML generation factored out into a reusable function, shared by both full grid render and targeted card updates.
- **Updated `.gitignore`**: Added `*.env` (block all `.env` files except `.env.example`), `opencode.json`, `.omo/`, `.sisyphus/`.
- **Updated `.env.example`**: Better organized with clearer section headers, comments, and proper default values matching code defaults.
- **Updated `README.md`**: Added architecture overview diagram, systemd service instructions, SSE events API reference, ffmpeg/PyMuPDF dependency notes, and clarified env var descriptions.
