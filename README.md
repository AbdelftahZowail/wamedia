# wamedia — Self-Hosted WhatsApp Media Browser

Browse, preview, and download media from your WhatsApp chats through a self-hosted web UI with real-time sync. Built on [wacli](https://github.com/openclaw/wacli) by [Steipete](https://github.com/steipete), powered by [whatsmeow](https://github.com/tulir/whatsmeow) by [Tulir](https://github.com/tulir).

![wamedia screenshot](screenshot.png)

## Quick Start

1. **Install wacli** — the WhatsApp CLI tool that syncs your chats
2. **Authenticate**: `wacli auth` (scan the QR code with WhatsApp)
3. **Install system dependencies**: `sudo apt install ffmpeg` (for video thumbnails)
4. **Install Python dependencies**: `pip install -r requirements.txt`
5. **Configure**: `cp .env.example .env`
6. **Run**: `python app.py`
7. **Setup**: Visit `http://localhost:8093/setup` to pick which chats to browse
8. **First login**: The initial password is printed to `stderr` on first startup (check your terminal or `journalctl -u wamedia`)

## Features

- 📅 Browse WhatsApp media — paginated, pages end on day boundaries
- 🔍 Filter by chat, media type, and search text
- 🎬 Video preview with in-browser playback (MP4 fast-start for streaming)
- 🖼 Image preview with lazy loading and auto-generated thumbnails
- 📥 Batch download — select multiple files, get a ZIP
- ⚡ **Real-time sync** — new messages appear instantly via SSE, auto-download starts immediately
- 📂 Self-hosted — your media, your machine
- 🔐 Password-protected web UI
- ⚙ Chat selector — pick which WhatsApp chats to browse on first run
- 📤 Manual upload with custom paths — stable hosting URLs
- ⏳ Per-file TTL or permanent storage
- 🔒 Secure downloads — require auth token for private files
- 📄 PDF thumbnails (via PyMuPDF)

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `FILEHOST_WHATSAPP_CHATS` | *(empty)* | Comma-separated chat JIDs (fallback; use `/setup` page) |
| `FILEHOST_UPLOAD_TOKEN` | *(empty)* | Bearer token for `/upload` (leave empty to disable manual uploads) |
| `FILEHOST_DIR` | `./data` | Where metadata (`meta.json`, `auth.json`, `config.json`) live; wa\_media/ and wa\_thumbnails/ subdirectories are created here |
| `FILEHOST_HOST` | `127.0.0.1` | Listen address |
| `FILEHOST_PORT` | `8093` | Listen port |
| `FILEHOST_BASE_URL` | `http://localhost:8093` | Public URL for generated download links |
| `FILEHOST_SECRET_KEY` | *(auto-generated)* | Flask session secret (set for persistence across restarts) |
| `FILEHOST_TTL` | `86400` | Default upload expiry in seconds (24h). Per-upload override with `ttl` param |
| `WACLI_BIN` | `wacli` | Path to wacli binary |
| `WACLI_DB` | `~/.wacli/wacli.db` | Path to wacli SQLite database |
| `WACLI_STORE` | `~/.wacli` | Path to wacli session store (contains session keys, media cache) |
| `WACLI_PROXY` | *(empty)* | Prefix command for routing wacli through a SOCKS5 proxy, e.g. `proxychains4 -q` |

### Datacenter / VPS Note

If your VPS IP gets blocked by WhatsApp (not guaranteed — many datacenter IPs work fine), route traffic through a SOCKS5 residential proxy:

```bash
# Install proxychains4 (apt install proxychains4)
WACLI_PROXY=proxychains4 -q
```

All wacli subprocess calls will then be tunneled through the proxy automatically.

## Architecture

```
WhatsApp → wacli sync --follow (store lock held)
             ├── Stores messages in ~/.wacli/wacli.db
             ├── Downloads media via --download-media to ~/.wacli/media/
             └── POSTs webhook to /api/whatsapp/webhook
                   │
                   ├── SSE broadcast to all browsers ("new_message")
                   ├── Polls ~/.wacli/media/, copies to wa_media/
                   ├── Registers in meta_wa.json, generates thumbnail
                   └── SSE broadcast ("media_stored") → UI refresh
```

## Running as a Service

The project includes a systemd unit:

```bash
sudo cp wamedia.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wamedia
```

View logs: `journalctl -u wamedia -f`

## API Reference

All endpoints except `/upload` and `/dl/*` require a login session cookie (log in via `/login` first).

### Upload

```
POST /upload
Auth: Bearer <token> or session cookie
```

| Field | Type | Description |
|---|---|---|
| `file` | file | The file to upload (required) |
| `path` | string | Custom URL path, e.g. `logos/company.png`. Supports nested directories. |
| `ttl` | integer | TTL in seconds. `0` = permanent. Omit for global default (`FILEHOST_TTL`). |
| `secure` | boolean | Protect download behind auth (`1`, `true`, `yes`). Public by default. |

| Query | Description |
|---|---|
| `?overwrite=true` | Overwrite existing file at the same path. Without this, duplicate paths return `409 Conflict`. |

**Response `201`:**
```json
{
  "url": "https://your-host/dl/logos/company.png",
  "id": "abc123...",
  "filename": "image.png",
  "expires_in_seconds": 86400
}
```

**Examples:**

```bash
# Stable hosting — permanent file at a predictable URL, overwritable anytime
curl -F "file=@logo.png" -F "path=brand/logo.png" -F "ttl=0" \
     -H "Authorization: Bearer $TOKEN" \
     "https://your-host/upload?overwrite=true"

# Private file — only downloadable with the token
curl -F "file=@secret.pdf" -F "secure=true" \
     -H "Authorization: Bearer $TOKEN" \
     https://your-host/upload

# Temporary public file with custom TTL
curl -F "file=@screenshot.png" -F "ttl=3600" \
     -H "Authorization: Bearer $TOKEN" \
     https://your-host/upload
```

### Download

```
GET /dl/<id-or-path>
Auth: only for secure files (Bearer token or session cookie)
```

Serves the file as attachment. Supports UUID-based IDs and custom paths (e.g. `/dl/brand/logo.png`).
Public by default — files tagged `secure=true` require the same Bearer token used for upload.
Returns `410 Gone` if TTL expired, `403 Forbidden` if secure + no auth, `404` if not found.

### Manage Uploads

```
GET /api/files
Auth: session cookie
```

Returns all non-expired uploads:
```json
[
  {
    "id": "abc123...",
    "filename": "image.png",
    "size": 42000,
    "size_formatted": "41.0 KB",
    "expires_in_sec": 86000,
    "expires_in_fmt": "23h 53m",
    "url": "https://your-host/dl/logos/company.png",
    "custom_path": "logos/company.png",
    "upload_time": "2026-06-05 22:42"
  }
]
```

`expires_in_fmt` shows `"permanent"` for TTL=0 files. `custom_path` is `null` for UUID-only uploads.

---

```
DELETE /api/files/<id>
Auth: session cookie
```

Deletes the file from disk and metadata. Returns `{"ok": true}`.

### WhatsApp Media

```
GET /api/whatsapp/media
Auth: session cookie
```

| Query | Description |
|---|---|
| `page` | Page number. `latest` for most recent (default) |
| `type` | Filter: `image`, `video`, `document` |
| `search` | Search filename or caption |
| `chat` | Filter by chat JID |

Returns paginated media items (pages split on day boundaries).

```
GET /api/whatsapp/storage
Auth: session cookie
```

Returns total stored media size and file count:
```json
{
  "total_size": 524288000,
  "size_formatted": "500.0 MB",
  "file_count": 42
}
```

```
POST /api/whatsapp/sync
Auth: session cookie
```

Syncs new messages from WhatsApp and auto-downloads new media. Returns `synced` count and storage stats.

```
POST /api/whatsapp/backfill
Auth: session cookie
```

Backfills chat history. Body: `{"chat": "<JID>", "count": 50}`

```
POST /api/whatsapp/download-all
Auth: session cookie
```

Downloads all unstored media from configured chats. Returns download/skip/fail counts.

```
POST /api/whatsapp/batch-download
Auth: session cookie
```

Downloads selected media as a ZIP. Body: `{"msg_ids": ["id1", "id2", ...]}` (max 20).

### Real-time Events (SSE)

```
GET /api/whatsapp/events
Auth: session cookie
```

Server-Sent Events stream. The browser connects to receive push notifications:

- `{"type": "new_message", "msg_id": "...", "media_type": "image"}` — a new media message arrived
- `{"type": "media_stored", "msg_id": "..."}` — the media has been downloaded and stored locally

The frontend uses these to bust the cache and refresh the media grid without polling.

## Built On

- [wacli](https://github.com/openclaw/wacli) by [Steipete](https://github.com/steipete) — WhatsApp CLI (pairs as a linked device, syncs to local SQLite)
- [whatsmeow](https://github.com/tulir/whatsmeow) by [Tulir](https://github.com/tulir) — Go WhatsApp Web library

## Dependencies

- Python 3.8+
- Flask ≥ 3.0
- bcrypt ≥ 4.0
- Pillow ≥ 10.0 (image thumbnails)
- PyMuPDF ≥ 1.23.0 (PDF thumbnails)
- ffmpeg (video thumbnails + MP4 fast-start)
- wacli ≥ 0.11.0

## License

MIT — see LICENSE