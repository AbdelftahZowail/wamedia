"""
FileHost — personal file host with WhatsApp media mirror.
Local-first approach with thumbnail generation and auto-download.
"""
import os
import json
import uuid
import time
import secrets
import threading
import subprocess
import zipfile
import io
import sqlite3
from pathlib import Path
from datetime import timedelta, datetime
from flask import (
    Flask, request, jsonify, send_file, abort,
    render_template, session, redirect, url_for, Response
)
from functools import wraps

# Load .env file if present
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _key not in os.environ:
                os.environ[_key] = _val

app = Flask(__name__)
app.secret_key = os.environ.get("FILEHOST_SECRET_KEY", secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# --- Config (all via env vars with sensible defaults) ---
HOME = Path.home()
DATA_DIR = Path(os.environ.get("FILEHOST_DIR", str(Path.cwd() / "data")))
AUTH_FILE = DATA_DIR / "auth.json"
META_FILE = DATA_DIR / "meta.json"
WA_META_FILE = DATA_DIR / "meta_wa.json"
WA_DATA_DIR = DATA_DIR / "wa_media"
THUMB_DIR = DATA_DIR / "wa_thumbnails"
CONFIG_FILE = DATA_DIR / "config.json"

# WhatsApp / wacli
WACLI_DB = Path(os.environ.get("WACLI_DB", str(HOME / ".wacli" / "wacli.db")))
WACLI_BIN = os.environ.get("WACLI_BIN", "wacli")
WACLI_STORE = os.environ.get("WACLI_STORE", str(HOME / ".wacli"))
WACLI_PROXY = os.environ.get("WACLI_PROXY", "").strip()
WHATSAPP_CHATS = os.environ.get("FILEHOST_WHATSAPP_CHATS", "")  # comma-separated

# Upload
UPLOAD_TOKEN = os.environ.get("FILEHOST_UPLOAD_TOKEN", "")
TTL_SECONDS = int(os.environ.get("FILEHOST_TTL", "86400"))

# Server
BASE_URL = os.environ.get("FILEHOST_BASE_URL", "http://localhost:8093")
HOST = os.environ.get("FILEHOST_HOST", "127.0.0.1")
PORT = int(os.environ.get("FILEHOST_PORT", "8093"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(WA_DATA_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)


def _wacli_cmd(*args):
    """Build wacli command list, wrapping with proxy if configured."""
    cmd = [WACLI_BIN] + list(args)
    if WACLI_PROXY:
        cmd = WACLI_PROXY.split() + cmd
    return cmd


def load_config():
    """Load config.json. Returns dict with defaults if missing."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(data):
    """Save config.json."""
    CONFIG_FILE.write_text(json.dumps(data, indent=2))

def _get_chats():
    """Get configured chat JIDs from config.json, falling back to env var."""
    cfg = load_config()
    if cfg.get("chats"):
        return list(cfg["chats"])
    if WHATSAPP_CHATS:
        return [c.strip() for c in WHATSAPP_CHATS.split(",") if c.strip()]
    return []

def _primary_chat():
    """Return the first configured chat, or None."""
    chats = _get_chats()
    return chats[0] if chats else None

def _get_chats_with_info():
    """Return list of {jid, name, kind} for all configured chats."""
    jids = _get_chats()
    if not jids or not WACLI_DB.exists():
        return [{"jid": j, "name": j, "kind": "unknown"} for j in jids]
    conn = sqlite3.connect(str(WACLI_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT jid, kind, name FROM chats WHERE jid IN ({','.join(['?']*len(jids))})",
        jids
    ).fetchall()
    conn.close()
    result = []
    seen = set()
    for r in rows:
        seen.add(r["jid"])
        result.append({"jid": r["jid"], "kind": r["kind"], "name": r["name"] or r["jid"]})
    for j in jids:
        if j not in seen:
            result.append({"jid": j, "name": j, "kind": "unknown"})
    return result


# ============================================================
#  Auth
# ============================================================

def load_auth():
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text())
    return {}

def save_auth(data):
    AUTH_FILE.write_text(json.dumps(data, indent=2))

def init_auth():
    """Create auth.json with a random password if it doesn't exist."""
    if not AUTH_FILE.exists():
        import bcrypt
        password = secrets.token_urlsafe(12)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        save_auth({"password_hash": hashed, "auth_stamp": secrets.token_hex(16)})
        import sys
        msg = f"\n[AUTH] Initial password: {password}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        return password
    return None

def _ensure_auth_stamp():
    """Add auth_stamp to existing auth.json if missing."""
    auth = load_auth()
    if auth and "auth_stamp" not in auth:
        auth["auth_stamp"] = secrets.token_hex(16)
        save_auth(auth)

def check_password(password):
    import bcrypt
    auth = load_auth()
    if not auth.get("password_hash"):
        return False
    return bcrypt.checkpw(password.encode(), auth["password_hash"].encode())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                abort(401)
            return redirect(url_for("login_page"))
        # Invalidate session if auth was reset (new auth_stamp)
        auth = load_auth()
        if session.get("auth_stamp") != auth.get("auth_stamp"):
            session.clear()
            if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                abort(401)
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ============================================================
#  Meta helpers (manual uploads)
# ============================================================

def load_meta():
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return {}

def save_meta(meta):
    META_FILE.write_text(json.dumps(meta, indent=2))

def cleanup_expired():
    meta = load_meta()
    now = time.time()
    expired = [fid for fid, f in meta.items() if 0 < f.get("expires", 0) < now]
    for fid in expired:
        filepath = DATA_DIR / (meta[fid].get("custom_path") or fid)
        if filepath.exists():
            filepath.unlink()
        del meta[fid]
    if expired:
        save_meta(meta)

def cleanup_loop():
    while True:
        time.sleep(1800)
        try:
            cleanup_expired()
        except Exception as e:
            print(f"[cleanup] Error: {e}")

threading.Thread(target=cleanup_loop, daemon=True).start()


# ============================================================
#  WhatsApp meta (stored media)
# ============================================================

def load_wa_meta():
    if WA_META_FILE.exists():
        return json.loads(WA_META_FILE.read_text())
    return {}

def save_wa_meta(meta):
    WA_META_FILE.write_text(json.dumps(meta, indent=2))


# ============================================================
#  WhatsApp helpers
# ============================================================

def format_size(size_bytes):
    if size_bytes is None:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

PER_PAGE = 50


# ============================================================
#  Diagnostics
# ============================================================

def _check_wacli_table(table_name):
    """Returns (ok: bool, conn_alive: bool) — does the table exist in wacli.db?"""
    conn = None
    try:
        conn = sqlite3.connect(str(WACLI_DB))
        conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
        return True, True
    except sqlite3.OperationalError:
        return False, True
    except Exception:
        return False, False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _safe_read_json(path):
    """Returns (parsed, error_str) — never raises."""
    try:
        with open(path) as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, ValueError) as e:
        return None, str(e)
    except OSError as e:
        return None, str(e)


def diagnose():
    """Return a list of structured issues describing why wamedia can't run normally.

    Each issue: {severity, code, title, message, fix, fix_command, fix_doc_url}
    Severity is "error" (blocks the app) or "warning" (degraded but usable).
    Safe to call on every request — no subprocess calls, only file/state checks.
    Ordered to early-return on the most fundamental issue so we don't pile on noise.
    """
    issues = []

    # ---- A1/A2: wacli binary present & executable ----
    wacli_path = Path(WACLI_BIN)
    if not wacli_path.exists():
        issues.append({
            "severity": "error",
            "code": "wacli_binary_missing",
            "title": "wacli binary not found",
            "message": f"wacli is not installed at {WACLI_BIN}. wamedia needs it to talk to WhatsApp.",
            "fix": "Install wacli (https://github.com/openclaw/wacli), then either move the binary to /tmp/wacli or set WACLI_BIN in .env to its actual path.",
            "fix_command": f"curl -L -o {WACLI_BIN} <release-url> && chmod +x {WACLI_BIN}",
            "fix_doc_url": "https://github.com/openclaw/wacli#install",
        })
        return issues  # can't check anything else without wacli

    if not os.access(str(wacli_path), os.X_OK):
        issues.append({
            "severity": "error",
            "code": "wacli_not_executable",
            "title": "wacli binary is not executable",
            "message": f"Found {WACLI_BIN} on disk but it isn't executable.",
            "fix": "Make it executable:",
            "fix_command": f"chmod +x {WACLI_BIN}",
        })
        return issues

    # ---- A3: wacli DB file missing ----
    if not WACLI_DB.exists():
        issues.append({
            "severity": "error",
            "code": "wacli_not_authenticated",
            "title": "wacli is not authenticated",
            "message": "wacli has no database yet — WhatsApp is not connected. The dashboard and any WhatsApp features will not work until you sign in.",
            "fix": "Scan the QR code with your phone to connect WhatsApp as a linked device. The WACLI_STORE env var is also passed so the session lands in the right place.",
            "fix_command": f"WACLI_STORE={WACLI_STORE} {WACLI_BIN} auth",
            "fix_doc_url": "https://github.com/openclaw/wacli#auth",
        })
        return issues

    # ---- A4: wacli DB exists but missing schema (empty / just-wiped state) ----
    table_ok, _ = _check_wacli_table("messages")
    if not table_ok:
        issues.append({
            "severity": "error",
            "code": "wacli_not_authenticated",
            "title": "wacli is not authenticated",
            "message": "The wacli database exists but is empty — WhatsApp is not connected. (This is the state after a fresh install or after wiping the wacli data dir.)",
            "fix": "Scan the QR code with your phone to connect WhatsApp as a linked device:",
            "fix_command": f"WACLI_STORE={WACLI_STORE} {WACLI_BIN} auth",
            "fix_doc_url": "https://github.com/openclaw/wacli#auth",
        })
        return issues

    # ---- A5: auth done but no chats synced yet ----
    chats_table_ok, _ = _check_wacli_table("chats")
    if chats_table_ok:
        try:
            conn = sqlite3.connect(str(WACLI_DB))
            chats_count = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            conn.close()
        except Exception:
            chats_count = -1
        if chats_count == 0:
            issues.append({
                "severity": "warning",
                "code": "wacli_no_sync",
                "title": "wacli hasn't synced any chats yet",
                "message": "WhatsApp is connected but no chat list has been downloaded. The dashboard will be empty until the first sync runs.",
                "fix": "Run a one-shot sync, or visit /setup to trigger it from the UI:",
                "fix_command": f"WACLI_STORE={WACLI_STORE} {WACLI_BIN} sync --once",
            })
            return issues  # no point checking the rest — there's no data

    # ---- A6: no chats configured ----
    if not _get_chats():
        issues.append({
            "severity": "warning",
            "code": "no_chats_configured",
            "title": "No chats selected",
            "message": "No WhatsApp chats are configured for browsing. The dashboard will redirect to /setup.",
            "fix": "Visit the setup page to pick which chats to mirror:",
            "fix_command": "Open https://{}/setup in your browser".format(request.host if 'request' in dir() else 'dl.shortformfunnels.com'),
        })
        # fall through to remaining checks (B*, C*, D*)

    # ---- B1/B2/B3: filesystem write permissions ----
    for label, path in [("DATA_DIR", DATA_DIR), ("WA_DATA_DIR", WA_DATA_DIR), ("THUMB_DIR", THUMB_DIR)]:
        if path.exists() and not os.access(str(path), os.W_OK):
            issues.append({
                "severity": "error",
                "code": f"{label.lower()}_not_writable",
                "title": f"{label} is not writable",
                "message": f"wamedia can't write to {path}. Uploads, downloads, and thumbnails will fail.",
                "fix": "Fix the directory permissions:",
                "fix_command": f"sudo chown -R ubuntu:ubuntu {path} && chmod 755 {path}",
            })

    # ---- C1/C2: auth.json ----
    if AUTH_FILE.exists():
        parsed, err = _safe_read_json(AUTH_FILE)
        if err and err != "missing":
            issues.append({
                "severity": "error",
                "code": "auth_json_corrupt",
                "title": "auth.json is corrupt",
                "message": f"wamedia can't read the auth file ({err}). You'll be unable to log in.",
                "fix": "Back it up, remove it, and restart the service. init_auth() will generate a new random password (printed to the journal):",
                "fix_command": f"sudo systemctl stop wamedia && mv {AUTH_FILE} {AUTH_FILE}.bak && sudo systemctl start wamedia && sudo journalctl -u wamedia -n 30 | grep 'Initial password'",
            })
    else:
        issues.append({
            "severity": "error",
            "code": "auth_json_missing",
            "title": "auth.json is missing",
            "message": "auth.json is missing. Normally init_auth() creates this on service start. The service may have started without filesystem write permission.",
            "fix": "Restart the service — init_auth() will create a new one and print the password to the journal:",
            "fix_command": "sudo systemctl restart wamedia && sudo journalctl -u wamedia -n 30 | grep 'Initial password'",
        })

    # ---- D2/D4/D5: meta.json / meta_wa.json / config.json corrupt JSON ----
    for label, path, code in [
        ("meta.json", META_FILE, "meta_json_corrupt"),
        ("meta_wa.json", WA_META_FILE, "meta_wa_json_corrupt"),
        ("config.json", CONFIG_FILE, "config_json_corrupt"),
    ]:
        if path.exists():
            parsed, err = _safe_read_json(path)
            if err and err != "missing":
                issues.append({
                    "severity": "error",
                    "code": code,
                    "title": f"{label} is corrupt",
                    "message": f"wamedia can't read {label} ({err}). The corresponding feature will be broken until this is fixed.",
                    "fix": f"Back it up and reset. New writes from wamedia will recreate the file as {{}}:",
                    "fix_command": f"cp {path} {path}.bak && echo '{{}}' > {path} && sudo systemctl restart wamedia",
                })

    # ---- B4: FILEHOST_BASE_URL sanity ----
    if not BASE_URL or not (BASE_URL.startswith("http://") or BASE_URL.startswith("https://")):
        issues.append({
            "severity": "warning",
            "code": "base_url_invalid",
            "title": "FILEHOST_BASE_URL is not set or invalid",
            "message": f"FILEHOST_BASE_URL={BASE_URL!r}. Download links generated by /upload will be broken.",
            "fix": "Set it in /home/ubuntu/wamedia/.env to your public domain:",
            "fix_command": "FILEHOST_BASE_URL=https://dl.shortformfunnels.com",
        })

    return issues


def render_diagnostic(issues, raw_exception=None, status=500):
    """Render the error.html page. Used by both the global error handler and route guards."""
    return render_template(
        "error.html",
        issues=issues or [],
        raw_exception=raw_exception,
    ), status


def require_wa():
    """Return a diagnostic-page Flask response if wacli is broken, else None.

    Use at the top of any route that depends on a working wacli:
        err = require_wa()
        if err is not None:
            return err
    """
    issues = diagnose()
    blockers = [i for i in issues if i["severity"] == "error"]
    if blockers:
        return render_diagnostic(blockers, status=500)
    return None


@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    """Catch-all: any unhandled exception renders the diagnostic page with the raw trace in a <details>."""
    # Pass through HTTPExceptions (404, 401, etc.) — only handle real 500s
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    issues = diagnose()
    try:
        import traceback as _tb
        tb = _tb.format_exc()
    except Exception:
        tb = repr(e)
    return render_diagnostic(issues, raw_exception=tb, status=500)


def get_wacli_media(page=1, media_type=None, search=None, chat_jids=None):
    """Query wacli DB — paginated, pages end on day boundaries."""
    if not WACLI_DB.exists():
        return [], 0, 0, None

    if chat_jids is None:
        chat_jids = _get_chats()
    if not chat_jids:
        return [], 0, 0, None

    conn = sqlite3.connect(str(WACLI_DB))
    conn.row_factory = sqlite3.Row

    jid_placeholders = ",".join(["?"] * len(chat_jids))
    where = f"""WHERE chat_jid IN ({jid_placeholders})
        AND media_type IS NOT NULL AND media_type != ''
        AND revoked = 0 AND deleted_for_me = 0"""
    params = list(chat_jids)

    if media_type:
        where += " AND media_type = ?"
        params.append(media_type)
    if search:
        where += " AND (filename LIKE ? OR media_caption LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    rows = conn.execute(
        f"""SELECT msg_id, chat_jid, chat_name, ts, media_type, mime_type, filename,
                  file_length, media_caption, text, from_me
           FROM messages {where}
           ORDER BY ts ASC
           LIMIT 10000""",
        params
    ).fetchall()
    conn.close()

    if not rows:
        return [], 0, 0, None

    # Build pages: group into days, never split a day across pages
    pages = []
    current_page = []
    current_count = 0
    i = 0

    while i < len(rows):
        row_day = datetime.fromtimestamp(rows[i]["ts"]).strftime("%Y-%m-%d")

        # Collect all rows for this day
        day_rows = []
        while i < len(rows) and datetime.fromtimestamp(rows[i]["ts"]).strftime("%Y-%m-%d") == row_day:
            day_rows.append(rows[i])
            i += 1

        # If adding this day would exceed PER_PAGE and the page isn't empty,
        # start a new page (day is never split)
        if current_count > 0 and current_count + len(day_rows) > PER_PAGE:
            pages.append(current_page)
            current_page = []
            current_count = 0

        current_page.extend(day_rows)
        current_count += len(day_rows)

    if current_page:
        pages.append(current_page)

    total_pages = len(pages)
    page = max(1, min(page, total_pages))
    page_rows = pages[page - 1]

    page_first_day = datetime.fromtimestamp(page_rows[0]["ts"]).strftime("%Y-%m-%d")
    page_last_day = datetime.fromtimestamp(page_rows[-1]["ts"]).strftime("%Y-%m-%d")
    date_label = page_first_day if page_first_day == page_last_day else f"{page_first_day} – {page_last_day}"

    wa_meta = load_wa_meta()
    items = []
    for r in page_rows:
        dt = datetime.fromtimestamp(r["ts"])
        stored = wa_meta.get(r["msg_id"])
        items.append({
            "msg_id": r["msg_id"],
            "chat_jid": r["chat_jid"],
            "chat_name": r["chat_name"] or r["chat_jid"],
            "ts": r["ts"],
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "date_day": dt.strftime("%a, %b %d"),
            "date_day_iso": dt.strftime("%Y-%m-%d"),
            "media_type": r["media_type"],
            "filename": r["filename"] or "",
            "file_length": r["file_length"] or 0,
            "size_formatted": format_size(r["file_length"] or 0),
            "caption": r["media_caption"] or r["text"] or "",
            "stored": stored is not None,
            "type_label": _type_label(r["media_type"], r["mime_type"]),
            "has_preview": _has_preview(r["media_type"], r["mime_type"] or ""),
        })

    return items, len(page_rows), total_pages, date_label


def _type_label(media_type, mime_type):
    if media_type == "video":
        return "🎬 Video"
    elif media_type == "image":
        return "🖼 Image"
    elif media_type == "document":
        if mime_type and "pdf" in mime_type:
            return "📄 PDF"
        elif mime_type and "sheet" in mime_type:
            return "📊 Sheet"
        return "📁 Document"
    return media_type or "File"


def _has_preview(media_type, mime_type=""):
    """Whether a thumbnail preview can be generated for this media type."""
    if media_type == "image":
        return True
    if media_type == "video":
        return True
    if media_type == "document" and mime_type and "pdf" in mime_type:
        return True
    return False


def download_media(msg_id):
    """Download actual file from WhatsApp, store in WA_DATA_DIR.
    Returns (stored_path, filename, mime_type) or (None, None, None)."""
    wa_meta = load_wa_meta()

    # Already stored?
    if msg_id in wa_meta:
        existing = WA_DATA_DIR / wa_meta[msg_id]["stored_filename"]
        if existing.exists():
            generate_thumbnail(msg_id, str(existing), wa_meta[msg_id].get("media_type", ""), wa_meta[msg_id].get("mime_type", ""))
            return str(existing), wa_meta[msg_id]["filename"], wa_meta[msg_id]["mime_type"]

    conn = sqlite3.connect(str(WACLI_DB))
    row = conn.execute(
        "SELECT chat_jid, filename, media_type, mime_type FROM messages WHERE msg_id = ?",
        (msg_id,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"[download] {msg_id}: not found in wacli DB")
        return None, None, None

    chat_jid, filename, media_type, mime_type = row

    # Determine extension
    if filename:
        ext = Path(filename).suffix or ".bin"
    else:
        ext = _ext_from_mime(mime_type)

    stored_filename = f"{msg_id}{ext}"
    stored_path = WA_DATA_DIR / stored_filename

    if stored_path.exists():
        # Already on disk but not in meta — register it
        print(f"[download] {msg_id}: already on disk ({stored_path}), registering in meta")
        wa_meta[msg_id] = {
            "filename": filename or f"whatsapp_{msg_id[:8]}{ext}",
            "mime_type": mime_type or "",
            "media_type": media_type or "",
            "size": stored_path.stat().st_size,
            "stored_filename": stored_filename,
            "stored_at": time.time(),
        }
        save_wa_meta(wa_meta)
        faststart_video(str(stored_path), mime_type)
        generate_thumbnail(msg_id, str(stored_path), media_type, mime_type)
        return str(stored_path), filename, mime_type or ""

    # Download via wacli
    try:
        print(f"[download] {msg_id}: downloading from WhatsApp (chat={chat_jid})...")
        result = subprocess.run([
            WACLI_BIN, "media", "download",
            "--chat", chat_jid,
            "--id", msg_id,
            "--output", str(stored_path),
            "--store", WACLI_STORE,
        ], capture_output=True, text=True, timeout=120)

        if result.returncode != 0 or not stored_path.exists():
            print(f"[download] {msg_id}: wacli failed (rc={result.returncode}, stderr={result.stderr.strip()})")
            return None, None, None

        size = stored_path.stat().st_size
        print(f"[download] {msg_id}: downloaded OK ({format_size(size)})")

        wa_meta[msg_id] = {
            "filename": filename or f"whatsapp_{msg_id[:8]}{ext}",
            "mime_type": mime_type or "",
            "media_type": media_type or "",
            "size": size,
            "stored_filename": stored_filename,
            "stored_at": time.time(),
        }
        save_wa_meta(wa_meta)
        faststart_video(str(stored_path), mime_type)
        generate_thumbnail(msg_id, str(stored_path), media_type, mime_type)
        return str(stored_path), filename, mime_type or ""

    except Exception as e:
        print(f"[download_media] Error {msg_id}: {e}")
        return None, None, None


def faststart_video(stored_path, mime_type=""):
    """Move moov atom to front of MP4 for fast streaming. Non-destructive copy."""
    if not stored_path or not os.path.exists(stored_path):
        return
    if mime_type and not mime_type.startswith("video/"):
        return
    ext = os.path.splitext(stored_path)[1].lower()
    if ext not in (".mp4", ".m4v", ".mov", ".3gp"):
        return
    tmp = stored_path + ".tmp"
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", stored_path, "-c", "copy", "-movflags", "+faststart", "-y", tmp],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, stored_path)
        elif os.path.exists(tmp):
            os.unlink(tmp)
    except Exception as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        print(f"[faststart] Error for {stored_path}: {e}")


def generate_thumbnail(msg_id, stored_path, media_type, mime_type=""):
    """Generate a 320px-wide JPEG thumbnail for a stored media file.
    Supports images (Pillow), PDFs (PyMuPDF), and videos (ffmpeg)."""
    thumb_path = THUMB_DIR / f"{msg_id}.jpg"
    if thumb_path.exists():
        return str(thumb_path)
    if not stored_path or not os.path.exists(stored_path):
        return None
    try:
        if media_type == "image":
            from PIL import Image
            img = Image.open(stored_path)
            img.thumbnail((320, 320))
            img.convert("RGB").save(str(thumb_path), "JPEG", quality=80)
            return str(thumb_path)
        elif media_type == "document" and mime_type and "pdf" in mime_type:
            import fitz
            doc = fitz.open(stored_path)
            if doc.page_count > 0:
                page = doc.load_page(0)
                mat = fitz.Matrix(320 / page.rect.width, 320 / page.rect.width)
                pix = page.get_pixmap(matrix=mat)
                pix.save(str(thumb_path))
            doc.close()
            if thumb_path.exists():
                return str(thumb_path)
            return None
        elif media_type == "video":
            result = subprocess.run([
                "ffmpeg", "-i", str(stored_path),
                "-vframes", "1",
                "-vf", "scale=320:-2",
                "-y", str(thumb_path)
            ], capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and thumb_path.exists():
                return str(thumb_path)
    except ImportError:
        print(f"[thumbnail] {msg_id}: missing library for {media_type}")
    except Exception as e:
        print(f"[thumbnail] Error for {msg_id}: {e}")
    return None


def get_thumbnail(msg_id):
    """Get thumbnail path for a msg_id if it exists."""
    thumb_path = THUMB_DIR / f"{msg_id}.jpg"
    if thumb_path.exists():
        return str(thumb_path)
    # Try to generate from stored media
    wa_meta = load_wa_meta()
    if msg_id in wa_meta:
        entry = wa_meta[msg_id]
        stored_path = WA_DATA_DIR / entry.get("stored_filename", "")
        if stored_path.exists():
            return generate_thumbnail(msg_id, str(stored_path), entry.get("media_type", ""), entry.get("mime_type", ""))
    return None


def get_wa_storage():
    """Return total size and file count of stored WhatsApp media."""
    wa_meta = load_wa_meta()
    total_size = 0
    file_count = 0
    for entry in wa_meta.values():
        stored_path = WA_DATA_DIR / entry.get("stored_filename", "")
        if stored_path.exists():
            total_size += entry.get("size", stored_path.stat().st_size)
            file_count += 1
    return total_size, file_count


def _ext_from_mime(mime_type):
    if not mime_type:
        return ".bin"
    if "mp4" in mime_type:
        return ".mp4"
    if "jpeg" in mime_type or "jpg" in mime_type:
        return ".jpg"
    if "png" in mime_type:
        return ".png"
    if "pdf" in mime_type:
        return ".pdf"
    return ".bin"


# ============================================================
#  Web routes
# ============================================================

@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    return render_template("login.html", has_password=AUTH_FILE.exists())

@app.route("/setup", methods=["GET"])
@login_required
def setup_page():
    """Chat selector — list all chats from wacli DB with checkboxes."""
    chats = []
    db_missing = not WACLI_DB.exists()
    configured = _get_chats()
    configured_set = set(configured)
    if not db_missing:
        conn = sqlite3.connect(str(WACLI_DB))
        conn.row_factory = sqlite3.Row
        # Join chats with groups to get names from either table
        rows = conn.execute("""
            SELECT c.jid, c.kind, COALESCE(c.name, g.name, c.jid) AS display_name,
                   c.last_message_ts, c.unread
            FROM chats c
            LEFT JOIN groups g ON c.jid = g.jid
            ORDER BY COALESCE(c.last_message_ts, 0) DESC
        """).fetchall()
        conn.close()
        for r in rows:
            last_ts = r["last_message_ts"]
            last_fmt = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M") if last_ts else "never"
            chats.append({
                "jid": r["jid"],
                "kind": r["kind"],
                "name": r["display_name"],
                "last_fmt": last_fmt,
                "unread": r["unread"] or 0,
            })
        # Selected chats first, then alphabetically
        chats.sort(key=lambda c: (c["jid"] not in configured_set, c["name"].lower()))
    return render_template("setup.html", chats=chats, configured=configured, db_missing=db_missing, is_initial=not configured)

@app.route("/api/setup/refresh", methods=["POST"])
@login_required
def setup_refresh():
    """Refresh chat metadata from WhatsApp without full sync."""
    if not WACLI_DB.exists():
        return jsonify({"ok": False, "error": "wacli database not found"}), 400
    try:
        result = subprocess.run(
            _wacli_cmd("sync", "--once", "--refresh-groups", "--refresh-contacts",
                       "--idle-exit", "5s", "--store", WACLI_STORE),
            capture_output=True, text=True, timeout=60
        )
        return jsonify({"ok": result.returncode == 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/setup", methods=["POST"])
@login_required
def setup_save():
    """Save selected chat JIDs to config.json."""
    jids = request.form.getlist("chats")
    if not jids:
        return render_template("setup.html",
            error="Please select at least one chat.",
            chats=[], configured=[], db_missing=not WACLI_DB.exists())
    save_config({"chats": jids})
    print(f"[setup] Saved chats: {jids}, spawning background download")
    threading.Thread(target=_download_new_media, args=[0], daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() if request.is_json else request.form
    password = data.get("password", "")

    if not AUTH_FILE.exists():
        return jsonify({"error": "No password set"}), 500

    if check_password(password):
        session.permanent = True
        session["authenticated"] = True
        auth = load_auth()
        session["auth_stamp"] = auth.get("auth_stamp")
        if request.is_json:
            return jsonify({"ok": True})
        return redirect(url_for("dashboard"))

    if request.is_json:
        return jsonify({"error": "Wrong password"}), 401
    return render_template("login.html", error="Wrong password", has_password=True)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/")
@login_required
def dashboard():
    err = require_wa()
    if err is not None:
        return err
    chats = _get_chats()
    if not chats:
        return redirect(url_for("setup_page"))
    _, _, total_pages, _ = get_wacli_media(page=999999)
    return render_template("dashboard.html",
        active_tab="whatsapp",
        wa_page=max(1, total_pages),
        base_url=BASE_URL,
        has_uploads=bool(UPLOAD_TOKEN),
        configured_chats=_get_chats_with_info())

@app.route("/whatsapp")
@login_required
def dashboard_whatsapp():
    err = require_wa()
    if err is not None:
        return err
    chats = _get_chats()
    if not chats:
        return redirect(url_for("setup_page"))
    page_raw = request.args.get("page", "latest", type=str)
    if not page_raw or page_raw == "latest":
        _, _, total_pages, _ = get_wacli_media(page=999999)
        wa_page = max(1, total_pages)
    else:
        wa_page = max(1, int(page_raw))
    return render_template("dashboard.html",
        active_tab="whatsapp",
        wa_page=wa_page,
        base_url=BASE_URL,
        has_uploads=bool(UPLOAD_TOKEN),
        configured_chats=_get_chats_with_info())


# ============================================================
#  Upload (retains Bearer token + adds session auth)
# ============================================================

@app.route("/upload", methods=["POST"])
def upload():
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {UPLOAD_TOKEN}":
        pass
    elif session.get("authenticated"):
        pass
    else:
        abort(403)

    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "no file selected"}), 400

    custom_path = request.form.get("path", "").strip()
    ttl_str = request.form.get("ttl", "").strip()
    secure = request.form.get("secure", "").strip().lower() in ("1", "true", "yes")
    overwrite = request.args.get("overwrite") == "true"

    import re
    if custom_path:
        if ".." in custom_path or custom_path.startswith("/"):
            return jsonify({"error": "invalid path"}), 400
        if not re.match(r'^[a-zA-Z0-9_\-/.]+$', custom_path):
            return jsonify({"error": "invalid path: only alphanumeric, hyphens, underscores, slashes, dots allowed"}), 400
        custom_path = custom_path.strip("/")

    if ttl_str:
        try:
            ttl_val = int(ttl_str)
            if ttl_val < 0:
                return jsonify({"error": "ttl must be >= 0"}), 400
        except ValueError:
            return jsonify({"error": "ttl must be an integer"}), 400
    else:
        ttl_val = TTL_SECONDS

    meta = load_meta()

    if custom_path:
        existing = any(
            entry.get("custom_path") == custom_path
            for entry in meta.values()
        )
        if existing and not overwrite:
            return jsonify({"error": "path already exists, use ?overwrite=true to replace"}), 409

    file_id = uuid.uuid4().hex

    if custom_path and overwrite:
        for key, entry in list(meta.items()):
            if entry.get("custom_path") == custom_path:
                del meta[key]
        old_filepath = DATA_DIR / custom_path
        if old_filepath.exists():
            old_filepath.unlink()

    if custom_path:
        full_path = DATA_DIR / custom_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        filepath = full_path
    else:
        filepath = DATA_DIR / file_id

    file.save(filepath)

    if ttl_val == 0:
        expires_val = 0
    else:
        expires_val = time.time() + ttl_val

    meta[file_id] = {
        "original_name": file.filename,
        "upload_time": time.time(),
        "expires": expires_val,
        "size": filepath.stat().st_size,
        "custom_path": custom_path or None,
        "ttl": ttl_val,
        "secure": secure,
    }
    save_meta(meta)

    url_path = custom_path if custom_path else file_id

    return jsonify({
        "url": f"{BASE_URL}/dl/{url_path}",
        "id": file_id,
        "filename": file.filename,
        "expires_in_seconds": ttl_val,
    }), 201


# ============================================================
#  Download (unchanged public route)
# ============================================================

@app.route("/dl/<path:file_id>", methods=["GET"])
def download_file(file_id):
    meta = load_meta()

    entry = meta.get(file_id)
    if not entry:
        for key, val in meta.items():
            if val.get("custom_path") == file_id:
                file_id = key
                entry = val
                break

    if not entry:
        abort(404)

    if entry.get("secure"):
        auth = request.headers.get("Authorization", "")
        if not (auth == f"Bearer {UPLOAD_TOKEN}" or session.get("authenticated")):
            abort(403)

    expires = entry.get("expires", 0)
    if expires != 0 and expires < time.time():
        filepath = DATA_DIR / (entry.get("custom_path") or file_id)
        if filepath.exists():
            filepath.unlink()
        del meta[file_id]
        save_meta(meta)
        abort(410, description="Expired")

    filepath = DATA_DIR / (entry.get("custom_path") or file_id)
    if not filepath.exists():
        abort(404)

    return send_file(
        filepath,
        download_name=entry["original_name"],
        as_attachment=True,
    )


# ============================================================
#  API — Manual uploads
# ============================================================

@app.route("/api/files")
@login_required
def api_files():
    meta = load_meta()
    now = time.time()
    files = []
    for fid, entry in meta.items():
        expires = entry.get("expires", 0)
        if expires != 0 and expires <= now:
            continue
        if expires == 0:
            remaining = 0
            expires_fmt = "permanent"
            expires_sec = 0
        else:
            remaining = int(expires - now)
            expires_fmt = f"{remaining // 3600}h {(remaining % 3600) // 60}m"
            expires_sec = remaining
        url_path = entry.get("custom_path") or fid
        files.append({
            "id": fid,
            "filename": entry["original_name"],
            "size": entry.get("size", 0),
            "size_formatted": format_size(entry.get("size", 0)),
            "expires_in_sec": expires_sec,
            "expires_in_fmt": expires_fmt,
            "url": f"{BASE_URL}/dl/{url_path}",
            "custom_path": entry.get("custom_path"),
            "upload_time": datetime.fromtimestamp(
                entry["upload_time"]
            ).strftime("%Y-%m-%d %H:%M"),
        })
    files.sort(key=lambda x: x["expires_in_sec"])
    return jsonify(files)

@app.route("/api/files/<file_id>", methods=["DELETE"])
@login_required
def api_delete_file(file_id):
    meta = load_meta()
    if file_id not in meta:
        abort(404)
    entry = meta[file_id]
    filepath = DATA_DIR / (entry.get("custom_path") or file_id)
    if filepath.exists():
        filepath.unlink()
    del meta[file_id]
    save_meta(meta)
    return jsonify({"ok": True})


# ============================================================
#  API — WhatsApp
# ============================================================

@app.route("/api/whatsapp/media")
@login_required
def api_whatsapp_media():
    err = require_wa()
    if err is not None:
        return err
    page_raw = request.args.get("page", "latest", type=str)
    if page_raw == "latest":
        page = 999999
    else:
        try:
            page = max(1, int(page_raw))
        except (ValueError, TypeError):
            page = 1
    media_type = request.args.get("type", None, type=str)
    search = request.args.get("search", None, type=str)
    chat_filter = request.args.get("chat", None, type=str)
    chat_jids = [chat_filter] if chat_filter else None
    items, day_total, total_pages, target_date = get_wacli_media(
        page=page, media_type=media_type, search=search, chat_jids=chat_jids
    )
    return jsonify({
        "items": items,
        "total": day_total,
        "page": page,
        "total_pages": total_pages,
        "date": target_date,
    })

@app.route("/api/whatsapp/storage")
@login_required
def api_whatsapp_storage():
    total_size, file_count = get_wa_storage()
    return jsonify({
        "total_size": total_size,
        "size_formatted": format_size(total_size),
        "file_count": file_count,
    })

@app.route("/wa/media/<msg_id>")
@login_required
def wa_serve_media(msg_id):
    """Serve stored WhatsApp media file (images, videos, docs)."""
    err = require_wa()
    if err is not None:
        return err
    stored_path, filename, mime_type = download_media(msg_id)
    if not stored_path:
        abort(404, description="Media not available — download failed")

    return send_file(
        stored_path,
        download_name=filename or f"whatsapp_{msg_id[:8]}",
        mimetype=mime_type or None,
        conditional=True,  # supports Range requests for video seeking
    )

@app.route("/wa/thumb/<msg_id>")
@login_required
def wa_serve_thumbnail(msg_id):
    """Serve a thumbnail for a WhatsApp media file."""
    err = require_wa()
    if err is not None:
        return err
    thumb_path = get_thumbnail(msg_id)
    if not thumb_path:
        abort(404)
    return send_file(thumb_path, mimetype="image/jpeg", conditional=True)


@app.route("/wa/view/<msg_id>")
@login_required
def wa_view(msg_id):
    """Dedicated preview page for a WhatsApp media file."""
    err = require_wa()
    if err is not None:
        return err
    conn = sqlite3.connect(str(WACLI_DB))
    row = conn.execute(
        "SELECT msg_id, ts, media_type, mime_type, filename, file_length, media_caption, text FROM messages WHERE msg_id = ?",
        (msg_id,)
    ).fetchone()
    conn.close()

    if not row:
        abort(404)

    wa_meta = load_wa_meta()
    stored = wa_meta.get(msg_id)

    info = {
        "msg_id": row[0],
        "date": datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M"),
        "media_type": row[2],
        "mime_type": row[3] or "",
        "filename": row[4] or "",
        "file_length": row[5] or 0,
        "size_formatted": format_size(row[5] or 0),
        "caption": row[6] or row[7] or "",
        "stored": stored is not None,
        "media_url": f"/wa/media/{msg_id}" if stored else None,
    }

    return render_template("wa_view.html", info=info, base_url=BASE_URL)

@app.route("/api/whatsapp/download/<msg_id>")
@login_required
def api_whatsapp_download(msg_id):
    """Download stored media (or download-on-demand if not stored)."""
    err = require_wa()
    if err is not None:
        return err
    stored_path, filename, mime_type = download_media(msg_id)
    if not stored_path:
        abort(503, description="Download failed")

    return send_file(
        stored_path,
        download_name=filename or f"whatsapp_{msg_id[:8]}",
        as_attachment=True,
    )

@app.route("/api/whatsapp/batch-download", methods=["POST"])
@login_required
def api_whatsapp_batch_download():
    err = require_wa()
    if err is not None:
        return err
    data = request.get_json()
    if not data or "msg_ids" not in data:
        return jsonify({"error": "Need msg_ids array"}), 400

    msg_ids = data["msg_ids"]
    if len(msg_ids) > 20:
        return jsonify({"error": "Max 20 files at a time"}), 400

    wa_meta = load_wa_meta()
    zip_buf = io.BytesIO()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for msg_id in msg_ids:
            if msg_id in wa_meta:
                stored_filename = wa_meta[msg_id]["stored_filename"]
                stored_path = WA_DATA_DIR / stored_filename
                if stored_path.exists():
                    zf.write(str(stored_path), wa_meta[msg_id]["filename"])
                    continue

            # Try downloading
            stored_path, filename, _ = download_media(msg_id)
            if stored_path:
                zf.write(stored_path, filename or f"whatsapp_{msg_id[:8]}")

    zip_buf.seek(0)

    if zip_buf.getbuffer().nbytes == 0:
        return jsonify({"error": "Nothing to download"}), 400

    return send_file(
        zip_buf,
        mimetype="application/zip",
        download_name="whatsapp_batch.zip",
        as_attachment=True,
    )

@app.route("/api/whatsapp/sync", methods=["POST"])
@login_required
def api_whatsapp_sync():
    """Sync new messages and auto-download new media."""
    err = require_wa()
    if err is not None:
        return err
    all_chats = _get_chats()
    if not all_chats:
        return jsonify({"error": "No WhatsApp chat configured"}), 400

    jid_placeholders = ",".join(["?"] * len(all_chats))
    conn = sqlite3.connect(str(WACLI_DB))
    before_count = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE chat_jid IN ({jid_placeholders}) AND media_type IS NOT NULL AND media_type != ''",
        all_chats
    ).fetchone()[0]
    conn.close()

    synced = 0
    try:
        result = subprocess.run([
            WACLI_BIN, "sync",
            "--once",
            "--idle-exit", "5s",
            "--store", WACLI_STORE,
        ], capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            conn = sqlite3.connect(str(WACLI_DB))
            after_count = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE chat_jid IN ({jid_placeholders}) AND media_type IS NOT NULL AND media_type != ''",
                all_chats
            ).fetchone()[0]
            conn.close()
            synced = after_count - before_count
    except Exception:
        pass

    # Auto-download all new media in background
    if synced > 0:
        threading.Thread(target=_download_new_media, args=[0], daemon=True).start()

    total_size, file_count = get_wa_storage()

    return jsonify({
        "ok": True,
        "synced": synced,
        "total": before_count + synced,
        "downloading": synced > 0,
        "storage": format_size(total_size),
        "file_count": file_count,
    })


def _download_new_media(limit=500):
    """Download unstored media items across all configured chats.
    limit=0 means no limit (download all unstored media)."""
    wa_meta = load_wa_meta()
    chat_jids = _get_chats()
    if not chat_jids:
        print("[bg-download] No chats configured, skipping")
        return
    try:
        conn = sqlite3.connect(str(WACLI_DB))
        jid_placeholders = ",".join(["?"] * len(chat_jids))
        query = f"""
            SELECT msg_id FROM messages
            WHERE chat_jid IN ({jid_placeholders})
              AND media_type IS NOT NULL AND media_type != ''
              AND revoked = 0 AND deleted_for_me = 0
            ORDER BY ts DESC
        """
        params = list(chat_jids)
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()

        total = len(rows)
        already = sum(1 for (msg_id,) in rows if msg_id in wa_meta)
        print(f"[bg-download] Found {total} media items ({already} already stored, {total - already} to download) for chats: {chat_jids}")

        for (msg_id,) in rows:
            if msg_id not in wa_meta:
                print(f"[bg-download] Downloading {msg_id}...")
                download_media(msg_id)
                wa_meta = load_wa_meta()  # Reload after each download
        print(f"[bg-download] Done — {get_wa_storage()[1]} files stored")
    except Exception as e:
        print(f"[bg-download] Error: {e}")


def _background_download_loop():
    """Background thread: periodically download all unstored media."""
    while True:
        print("[bg-loop] Checking for new media to download...")
        try:
            _download_new_media(limit=0)
        except Exception as e:
            print(f"[bg-loop] Error: {e}")
        time.sleep(300)


threading.Thread(target=_background_download_loop, daemon=True).start()

@app.route("/api/whatsapp/backfill", methods=["POST"])
@login_required
def api_whatsapp_backfill():
    """Backfill chat history via wacli."""
    err = require_wa()
    if err is not None:
        return err
    data = request.get_json()
    if not data or "chat" not in data:
        return jsonify({"error": "Need {\"chat\": \"JID\", \"count\": 50}"}), 400
    chat = data["chat"]
    count = int(data.get("count", 50))
    try:
        result = subprocess.run(
            _wacli_cmd("history", "backfill",
                "--chat", chat,
                "--count", str(count),
                "--store", WACLI_STORE,
                "--idle-exit", "5s"),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500

        # Trigger background download of all new media that was backfilled
        threading.Thread(target=_download_new_media, args=[0], daemon=True).start()

        return jsonify({"ok": True, "chat": chat, "count": count})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/whatsapp/download-all", methods=["POST"])
@login_required
def api_download_all_media():
    """Download ALL media from all configured chats that isn't stored yet."""
    err = require_wa()
    if err is not None:
        return err
    chat_jids = _get_chats()
    if not chat_jids:
        return jsonify({"error": "No WhatsApp chat configured"}), 400

    jid_placeholders = ",".join(["?"] * len(chat_jids))
    conn = sqlite3.connect(str(WACLI_DB))
    rows = conn.execute(f"""
        SELECT msg_id FROM messages
        WHERE chat_jid IN ({jid_placeholders})
          AND media_type IS NOT NULL AND media_type != ''
          AND revoked = 0 AND deleted_for_me = 0
        ORDER BY ts DESC
    """, chat_jids).fetchall()
    conn.close()

    wa_meta = load_wa_meta()
    downloaded = 0
    skipped = 0
    failed = 0

    for (msg_id,) in rows:
        if msg_id in wa_meta:
            skipped += 1
            continue
        stored_path, _, _ = download_media(msg_id)
        if stored_path:
            downloaded += 1
        else:
            failed += 1

    total_size, file_count = get_wa_storage()

    return jsonify({
        "ok": True,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "storage": format_size(total_size),
        "file_count": file_count,
    })


# ============================================================
#  Entry
# ============================================================

# Initialize auth on first import (works with both python app.py and flask run)
_init_password = init_auth()
_ensure_auth_stamp()

if __name__ == "__main__":
    cleanup_expired()
    app.run(host=HOST, port=PORT)
