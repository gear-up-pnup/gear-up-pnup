#!/usr/bin/env python3
"""
MEB Career Bootcamp 2026 - server pendaftaran, admin, dan absensi QR.
Jurusan Teknik Mesin - Politeknik Negeri Ujung Pandang

Zero dependency: hanya Python 3.9+ (stdlib). Database SQLite dibuat otomatis
di data/bootcamp.db saat server pertama kali dijalankan. Tidak perlu XAMPP /
Laragon / Supabase / pip install apa pun.

Jalankan:   python server.py
Opsi:       --host 0.0.0.0 --port 8000 --cert cert.pem --key key.pem
Bantuan:    python server.py --help
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DB_PATH = Path(os.environ.get("BOOTCAMP_DB", BASE / "data" / "bootcamp.db"))

WITA = timezone(timedelta(hours=8))  # Makassar
SESSION_TTL = 8 * 3600
MAX_BODY = 64 * 1024
QR_PREFIX = "MEB26."
PBKDF2_ITER = 310_000

STATUS_LABEL = {"calon_alumni": "Calon Alumni", "alumni_baru": "Alumni Baru"}
INTEREST_LABEL = {
    "manufaktur": "Manufaktur & Produksi",
    "energi": "Energi, Migas & Pembangkit",
    "otomotif": "Otomotif & Alat Berat",
    "perawatan": "Perawatan & Konstruksi",
    "wirausaha": "Wirausaha",
    "studi_lanjut": "Studi Lanjut",
    "lainnya": "Lainnya",
}

DEFAULT_SETTINGS = {
    "event_title": "Mechanical Engineering Career Bootcamp",
    "event_subtitle": "Pembekalan Karier & Kesiapan Dunia Kerja untuk Alumni Baru dan Calon Alumni "
    "Jurusan Teknik Mesin Tahun 2026",
    "organizer": "Jurusan Teknik Mesin, Politeknik Negeri Ujung Pandang",
    "event_date": "",
    "event_time": "",
    "venue": "",
    "contact_name": "",
    "contact_phone": "",
    "quota": "0",
    "reg_open": "1",
    "reg_deadline": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin','petugas')),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    admin_id   INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_no        TEXT UNIQUE,
    code          TEXT NOT NULL UNIQUE,
    full_name     TEXT NOT NULL,
    nim           TEXT NOT NULL,
    email         TEXT NOT NULL,
    phone         TEXT NOT NULL,
    program_studi TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('calon_alumni','alumni_baru')),
    grad_year     INTEGER NOT NULL,
    interest      TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_participants_nim   ON participants (upper(nim));
CREATE UNIQUE INDEX IF NOT EXISTS ux_participants_email ON participants (lower(email));
CREATE INDEX IF NOT EXISTS ix_participants_created ON participants (created_at);
CREATE TABLE IF NOT EXISTS attendance (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL UNIQUE REFERENCES participants(id) ON DELETE CASCADE,
    checked_in_at  TEXT NOT NULL,
    checked_by     TEXT NOT NULL,
    method         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)  # autocommit; tx() eksplisit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc, bytes.fromhex(dk))
    except Exception:  # noqa: BLE001
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def init_db() -> tuple[str, str] | None:
    """Buat database + tabel + pengaturan awal + admin pertama. Return kredensial bila admin baru dibuat."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    created = None
    if conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
        user = os.environ.get("ADMIN_USER", "admin")
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        conn.execute(
            "INSERT INTO admins(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (user, hash_password(pw), now_iso()),
        )
        created = (user, pw)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    conn.close()
    return created


def get_settings(conn: sqlite3.Connection) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return s


def log_activity(conn, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO activity_log(ts, actor, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), actor, action, detail[:300]),
    )


# --------------------------------------------------------------------------- #
# Validasi
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, message: str, fields: dict | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.fields, self.extra = status, message, fields or {}, extra or {}


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
NIM_RE = re.compile(r"^[A-Za-z0-9.\-]{5,20}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(v, maxlen: int) -> str:
    v = CTRL_RE.sub("", str(v if v is not None else "")).strip()
    return re.sub(r"[ \t]{2,}", " ", v)[:maxlen]


def normalize_phone(raw: str) -> str:
    p = re.sub(r"[\s\-().]", "", raw)
    if p.startswith("+62"):
        p = "0" + p[3:]
    elif p.startswith("62") and len(p) > 10:
        p = "0" + p[2:]
    return p


def validate_participant(data: dict, partial: bool = False) -> dict:
    errs: dict[str, str] = {}
    out: dict = {}

    name = clean(data.get("full_name"), 100)
    if len(name) < 3 or not re.search(r"[A-Za-z]", name):
        errs["full_name"] = "Nama lengkap minimal 3 karakter."
    out["full_name"] = name

    nim = clean(data.get("nim"), 20).upper()
    if not NIM_RE.match(nim):
        errs["nim"] = "NIM tidak valid (5-20 karakter huruf/angka)."
    out["nim"] = nim

    email = clean(data.get("email"), 120).lower()
    if not EMAIL_RE.match(email):
        errs["email"] = "Format email tidak valid."
    out["email"] = email

    phone = normalize_phone(clean(data.get("phone"), 25))
    if not re.fullmatch(r"0\d{8,14}", phone):
        errs["phone"] = "Nomor WhatsApp tidak valid. Contoh: 0812 3456 7890."
    out["phone"] = phone

    prodi = clean(data.get("program_studi"), 80)
    if len(prodi) < 2:
        errs["program_studi"] = "Program studi wajib diisi."
    out["program_studi"] = prodi

    status = clean(data.get("status"), 20)
    if status not in STATUS_LABEL:
        errs["status"] = "Pilih status Anda."
    out["status"] = status

    try:
        gy = int(data.get("grad_year"))
        if not 2020 <= gy <= 2030:
            raise ValueError
    except (TypeError, ValueError):
        errs["grad_year"] = "Tahun lulus harus antara 2020 dan 2030."
        gy = 0
    out["grad_year"] = gy

    interest = clean(data.get("interest"), 20)
    if interest not in INTEREST_LABEL:
        errs["interest"] = "Pilih bidang karier yang diminati."
    out["interest"] = interest

    out["note"] = clean(data.get("note"), 300)

    if errs:
        raise ApiError(422, "Periksa kembali isian formulir.", errs)
    return out


def row_participant(r: sqlite3.Row, include_code: bool = True) -> dict:
    d = {
        "id": r["id"],
        "reg_no": r["reg_no"],
        "full_name": r["full_name"],
        "nim": r["nim"],
        "email": r["email"],
        "phone": r["phone"],
        "program_studi": r["program_studi"],
        "status": r["status"],
        "grad_year": r["grad_year"],
        "interest": r["interest"],
        "note": r["note"],
        "created_at": r["created_at"],
        "checked_in_at": r["checked_in_at"] if "checked_in_at" in r.keys() else None,
    }
    if include_code:
        d["code"] = r["code"]
    return d


PARTICIPANT_SELECT = """
SELECT p.*, a.checked_in_at AS checked_in_at, a.checked_by AS checked_by, a.method AS method
FROM participants p LEFT JOIN attendance a ON a.participant_id = p.id
"""


# --------------------------------------------------------------------------- #
# Rate limiting (in-memory)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self):
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _prune(self, key: str, window: float) -> list[float]:
        now = time.time()
        q = [t for t in self.hits.get(key, []) if now - t < window]
        self.hits[key] = q
        return q

    def allow(self, key: str, limit: int, window: float) -> bool:
        with self.lock:
            q = self._prune(key, window)
            if len(q) >= limit:
                return False
            q.append(time.time())
            return True

    def blocked(self, key: str, limit: int, window: float) -> bool:
        with self.lock:
            return len(self._prune(key, window)) >= limit

    def record(self, key: str) -> None:
        with self.lock:
            self.hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)


LIMITER = RateLimiter()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
ROUTES: list[tuple[str, re.Pattern, str | None, callable]] = []


def route(method: str, pattern: str, auth: str | None = None):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), auth, fn))
        return fn

    return deco


PAGES = {"/": "index.html", "/ticket": "ticket.html", "/admin": "admin.html"}
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; media-src 'self' blob:; connect-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class Handler(BaseHTTPRequestHandler):
    server_version = "MEBBootcamp/1.0"
    protocol_version = "HTTP/1.1"

    # ---- logging -------------------------------------------------------- #
    def log_message(self, fmt, *args):
        line = fmt % args
        line = re.sub(r"(/api/ticket/)[^\s?]+", r"\1***", line)
        sys.stderr.write(f"[{datetime.now(WITA):%H:%M:%S}] {self.client_address[0]} {line}\n")

    # ---- helpers -------------------------------------------------------- #
    @property
    def ip(self) -> str:
        return self.client_address[0]

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", CSP)

    def send_bytes(self, status: int, body: bytes, ctype: str, headers: dict | None = None, cookies: list | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, obj, cookies: list | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_bytes(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"}, cookies)

    def cookie_flags(self) -> str:
        secure = "; Secure" if (self.server.is_https or self.headers.get("X-Forwarded-Proto") == "https") else ""
        return f"; Path=/; HttpOnly; SameSite=Strict{secure}"

    # ---- auth ----------------------------------------------------------- #
    def current_admin(self, conn) -> sqlite3.Row | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie(raw)
            sid = c["sid"].value
        except Exception:  # noqa: BLE001
            return None
        th = hashlib.sha256(sid.encode()).hexdigest()
        return conn.execute(
            "SELECT a.* FROM sessions s JOIN admins a ON a.id = s.admin_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (th, int(time.time())),
        ).fetchone()

    def check_csrf(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if int(self.headers.get("Content-Length") or 0) > 0 and ctype != "application/json":
            raise ApiError(415, "Content-Type harus application/json.")
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            if urlparse(origin).netloc != host:
                raise ApiError(403, "Permintaan lintas-origin ditolak.")

    # ---- dispatch ------------------------------------------------------- #
    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "Permintaan terlalu besar.")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "JSON tidak valid.")
        if not isinstance(data, dict):
            raise ApiError(400, "Body harus berupa objek JSON.")
        return data

    def handle_any(self):
        url = urlparse(self.path)
        path = url.path
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if path.startswith("/api/"):
                self.handle_api(path)
            elif self.command in ("GET", "HEAD"):
                self.serve_static(path)
            else:
                raise ApiError(405, "Metode tidak diizinkan.")
        except ApiError as e:
            body = {"error": e.message, "fields": e.fields, **e.extra}
            self.send_json(e.status, body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[ERROR] {type(e).__name__}: {e}\n")
            import traceback

            traceback.print_exc()
            try:
                self.send_json(500, {"error": "Terjadi kesalahan pada server."})
            except Exception:  # noqa: BLE001
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

    def handle_api(self, path: str):
        for method, pattern, auth, fn in ROUTES:
            m = pattern.match(path)
            if m and method == self.command:
                break
        else:
            if any(p.match(path) for _, p, _, _ in ROUTES):
                raise ApiError(405, "Metode tidak diizinkan.")
            raise ApiError(404, "Endpoint tidak ditemukan.")

        body = {}
        if self.command in ("POST", "PUT", "DELETE"):
            self.check_csrf()
            body = self._read_body()
        conn = db()
        try:
            admin = None
            if auth:
                admin = self.current_admin(conn)
                if not admin:
                    raise ApiError(401, "Sesi berakhir. Silakan masuk kembali.")
                if auth == "admin" and admin["role"] != "admin":
                    raise ApiError(403, "Akses ditolak untuk peran Anda.")
            fn(self, conn, admin, m, body)
        finally:
            conn.close()

    # ---- static --------------------------------------------------------- #
    def serve_static(self, path: str):
        rel = PAGES.get(path.rstrip("/") or "/", None)
        if rel is None:
            rel = unquote(path).lstrip("/")
        target = (STATIC / rel).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            raise ApiError(404, "Halaman tidak ditemukan.")
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        cache = "public, max-age=604800" if target.suffix in (".woff2", ".png", ".jpg", ".svg", ".ico") or "vendor" in target.parts else "no-cache"
        self.send_bytes(200, target.read_bytes(), ctype, {"Cache-Control": cache})


# --------------------------------------------------------------------------- #
# Endpoint publik
# --------------------------------------------------------------------------- #
def registration_state(conn, s: dict) -> tuple[str, int]:
    registered = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    quota = int(s.get("quota") or 0)
    if s.get("reg_open") != "1":
        return "closed", registered
    if s.get("reg_deadline"):
        try:
            if datetime.now(WITA).date() > date.fromisoformat(s["reg_deadline"]):
                return "deadline", registered
        except ValueError:
            pass
    if quota > 0 and registered >= quota:
        return "full", registered
    return "open", registered


PUBLIC_KEYS = ("event_title", "event_subtitle", "organizer", "event_date", "event_time", "venue",
               "contact_name", "contact_phone", "reg_deadline")


@route("GET", "/api/public/event")
def api_event(h, conn, admin, m, body):
    s = get_settings(conn)
    state, registered = registration_state(conn, s)
    out = {k: s[k] for k in PUBLIC_KEYS}
    out.update(
        quota=int(s["quota"] or 0),
        registered=registered,
        state=state,
        interests=INTEREST_LABEL,
        statuses=STATUS_LABEL,
    )
    h.send_json(200, out)


@route("POST", "/api/register")
def api_register(h, conn, admin, m, body):
    if not LIMITER.allow(f"reg:{h.ip}", 12, 3600):
        raise ApiError(429, "Terlalu banyak percobaan pendaftaran. Coba lagi nanti.")
    if not body.get("consent"):
        raise ApiError(422, "Anda harus menyetujui pernyataan data.", {"consent": "Persetujuan wajib dicentang."})
    if clean(body.get("website"), 50):  # honeypot
        raise ApiError(400, "Permintaan ditolak.")
    data = validate_participant(body)
    s = get_settings(conn)
    state, _ = registration_state(conn, s)
    if state != "open":
        msg = {"closed": "Pendaftaran saat ini ditutup.", "deadline": "Batas waktu pendaftaran telah berakhir.",
               "full": "Kuota peserta telah penuh."}[state]
        raise ApiError(409, msg, extra={"state": state})
    code = secrets.token_urlsafe(12)
    ts = now_iso()
    try:
        with tx(conn):
            s = get_settings(conn)
            state, registered = registration_state(conn, s)
            if state != "open":
                raise ApiError(409, "Kuota peserta telah penuh.", extra={"state": state})
            for col, label in (("nim", "NIM"), ("email", "Email")):
                dup = conn.execute(f"SELECT 1 FROM participants WHERE lower({col}) = lower(?)", (data[col],)).fetchone()
                if dup:
                    raise ApiError(409, f"{label} sudah terdaftar.", {col: f"{label} ini sudah terdaftar. Gunakan 'Cek Tiket' untuk melihat tiket Anda."})
            cur = conn.execute(
                "INSERT INTO participants(code, full_name, nim, email, phone, program_studi, status, grad_year, "
                "interest, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, data["full_name"], data["nim"], data["email"], data["phone"], data["program_studi"],
                 data["status"], data["grad_year"], data["interest"], data["note"], ts, ts),
            )
            reg_no = f"MEB26-{cur.lastrowid:04d}"
            conn.execute("UPDATE participants SET reg_no = ? WHERE id = ?", (reg_no, cur.lastrowid))
    except sqlite3.IntegrityError:
        raise ApiError(409, "NIM atau email sudah terdaftar.")
    h.send_json(201, {"ok": True, "code": code, "reg_no": reg_no, "full_name": data["full_name"]})


@route("POST", "/api/ticket/lookup")
def api_ticket_lookup(h, conn, admin, m, body):
    if not LIMITER.allow(f"lookup:{h.ip}", 10, 600):
        raise ApiError(429, "Terlalu banyak percobaan. Coba lagi dalam beberapa menit.")
    nim = clean(body.get("nim"), 20).upper()
    email = clean(body.get("email"), 120).lower()
    r = conn.execute("SELECT code FROM participants WHERE upper(nim) = ? AND lower(email) = ?", (nim, email)).fetchone()
    if not r:
        raise ApiError(404, "Data tidak ditemukan. Pastikan NIM dan email sama dengan saat mendaftar.")
    h.send_json(200, {"code": r["code"]})


@route("GET", r"/api/ticket/(?P<code>[A-Za-z0-9_\-]{8,40})")
def api_ticket(h, conn, admin, m, body):
    if not LIMITER.allow(f"ticket:{h.ip}", 60, 600):
        raise ApiError(429, "Terlalu banyak permintaan.")
    r = conn.execute(PARTICIPANT_SELECT + " WHERE p.code = ?", (m["code"],)).fetchone()
    if not r:
        raise ApiError(404, "Tiket tidak ditemukan.")
    s = get_settings(conn)
    h.send_json(200, {
        "code": r["code"], "reg_no": r["reg_no"], "full_name": r["full_name"], "nim": r["nim"],
        "program_studi": r["program_studi"], "status": r["status"], "checked_in_at": r["checked_in_at"],
        "qr": QR_PREFIX + r["code"],
        "event": {k: s[k] for k in ("event_title", "organizer", "event_date", "event_time", "venue")},
    })


# --------------------------------------------------------------------------- #
# Auth admin
# --------------------------------------------------------------------------- #
@route("POST", "/api/admin/login")
def api_login(h, conn, admin, m, body):
    username = clean(body.get("username"), 50)
    password = str(body.get("password") or "")[:200]
    k_ip, k_user = f"login-ip:{h.ip}", f"login-user:{username.lower()}"
    if LIMITER.blocked(k_ip, 10, 600) or LIMITER.blocked(k_user, 5, 600):
        raise ApiError(429, "Terlalu banyak percobaan masuk. Coba lagi dalam 10 menit.")
    r = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
    ok = verify_password(password, r["password_hash"] if r else _DUMMY_HASH)
    if not (r and ok):
        LIMITER.record(k_ip)
        LIMITER.record(k_user)
        raise ApiError(401, "Username atau kata sandi salah.")
    LIMITER.clear(k_user)
    sid = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token_hash, admin_id, expires_at) VALUES (?,?,?)",
                 (hashlib.sha256(sid.encode()).hexdigest(), r["id"], int(time.time()) + SESSION_TTL))
    log_activity(conn, r["username"], "login", h.ip)
    cookie = f"sid={sid}; Max-Age={SESSION_TTL}{h.cookie_flags()}"
    h.send_json(200, {"username": r["username"], "role": r["role"]}, cookies=[cookie])


@route("POST", "/api/admin/logout")
def api_logout(h, conn, admin, m, body):
    try:
        sid = SimpleCookie(h.headers.get("Cookie") or "")["sid"].value
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(sid.encode()).hexdigest(),))
    except Exception:  # noqa: BLE001
        pass
    h.send_json(200, {"ok": True}, cookies=[f"sid=; Max-Age=0{h.cookie_flags()}"])


@route("GET", "/api/admin/me", auth="staff")
def api_me(h, conn, admin, m, body):
    h.send_json(200, {"username": admin["username"], "role": admin["role"]})


@route("POST", "/api/admin/password", auth="staff")
def api_password(h, conn, admin, m, body):
    cur, new = str(body.get("current") or ""), str(body.get("new") or "")
    if not verify_password(cur, admin["password_hash"]):
        raise ApiError(422, "Kata sandi saat ini salah.", {"current": "Kata sandi saat ini salah."})
    if len(new) < 8:
        raise ApiError(422, "Kata sandi baru minimal 8 karakter.", {"new": "Minimal 8 karakter."})
    conn.execute("UPDATE admins SET password_hash = ? WHERE id = ?", (hash_password(new), admin["id"]))
    log_activity(conn, admin["username"], "ganti_password")
    h.send_json(200, {"ok": True})


# --------------------------------------------------------------------------- #
# Admin: statistik & peserta
# --------------------------------------------------------------------------- #
@route("GET", "/api/admin/stats", auth="staff")
def api_stats(h, conn, admin, m, body):
    s = get_settings(conn)
    total = conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    present = conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
    by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM participants GROUP BY status")}
    by_interest = {r["interest"]: r["n"] for r in conn.execute("SELECT interest, COUNT(*) n FROM participants GROUP BY interest")}
    # pendaftaran per hari (WITA) 14 hari terakhir
    today = datetime.now(WITA).date()
    days = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    per_day = {d.isoformat(): 0 for d in days}
    for r in conn.execute("SELECT created_at FROM participants WHERE created_at >= ?",
                          ((datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),)):
        d = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(WITA).date().isoformat()
        if d in per_day:
            per_day[d] += 1
    recent = [
        {"full_name": r["full_name"], "nim": r["nim"], "reg_no": r["reg_no"], "checked_in_at": r["checked_in_at"], "method": r["method"]}
        for r in conn.execute(
            "SELECT p.full_name, p.nim, p.reg_no, a.checked_in_at, a.method FROM attendance a "
            "JOIN participants p ON p.id = a.participant_id ORDER BY a.checked_in_at DESC, a.id DESC LIMIT 8")
    ]
    state, _ = registration_state(conn, s)
    h.send_json(200, {
        "total": total, "present": present, "absent": total - present,
        "quota": int(s["quota"] or 0), "state": state,
        "by_status": by_status, "by_interest": by_interest, "per_day": per_day, "recent": recent,
    })


def build_filters(q: dict) -> tuple[str, list]:
    where, args = [], []
    term = clean(q.get("q"), 80)
    if term:
        like = f"%{term.replace('%', '').replace('_', '')}%"
        where.append("(p.full_name LIKE ? OR p.nim LIKE ? OR p.email LIKE ? OR p.reg_no LIKE ? OR p.phone LIKE ? OR p.program_studi LIKE ?)")
        args += [like] * 6
    if q.get("status") in STATUS_LABEL:
        where.append("p.status = ?")
        args.append(q["status"])
    if q.get("interest") in INTEREST_LABEL:
        where.append("p.interest = ?")
        args.append(q["interest"])
    if q.get("att") == "present":
        where.append("a.id IS NOT NULL")
    elif q.get("att") == "absent":
        where.append("a.id IS NULL")
    return (" WHERE " + " AND ".join(where)) if where else "", args


SORTS = {
    "created": "p.created_at", "name": "p.full_name COLLATE NOCASE", "nim": "p.nim",
    "status": "p.status", "reg": "p.id", "checkin": "a.checked_in_at",
}


@route("GET", "/api/admin/participants", auth="staff")
def api_participants(h, conn, admin, m, body):
    q = h.query
    where, args = build_filters(q)
    try:
        page = max(1, int(q.get("page", 1)))
        per = min(100, max(5, int(q.get("per", 20))))
    except ValueError:
        page, per = 1, 20
    order = SORTS.get(q.get("sort", "created"), "p.created_at")
    direction = "ASC" if q.get("dir") == "asc" else "DESC"
    total = conn.execute(f"SELECT COUNT(*) FROM participants p LEFT JOIN attendance a ON a.participant_id = p.id{where}", args).fetchone()[0]
    rows = conn.execute(
        f"{PARTICIPANT_SELECT}{where} ORDER BY {order} {direction}, p.id DESC LIMIT ? OFFSET ?",
        args + [per, (page - 1) * per],
    ).fetchall()
    h.send_json(200, {
        "items": [row_participant(r, include_code=admin["role"] == "admin") for r in rows],
        "total": total, "page": page, "per": per, "pages": max(1, -(-total // per)),
    })


@route("POST", "/api/admin/participants", auth="admin")
def api_participant_create(h, conn, admin, m, body):
    data = validate_participant(body)
    ts = now_iso()
    try:
        with tx(conn):
            for col, label in (("nim", "NIM"), ("email", "Email")):
                if conn.execute(f"SELECT 1 FROM participants WHERE lower({col}) = lower(?)", (data[col],)).fetchone():
                    raise ApiError(409, f"{label} sudah terdaftar.", {col: f"{label} sudah terdaftar."})
            cur = conn.execute(
                "INSERT INTO participants(code, full_name, nim, email, phone, program_studi, status, grad_year, "
                "interest, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (secrets.token_urlsafe(12), data["full_name"], data["nim"], data["email"], data["phone"],
                 data["program_studi"], data["status"], data["grad_year"], data["interest"], data["note"], ts, ts),
            )
            conn.execute("UPDATE participants SET reg_no = ? WHERE id = ?", (f"MEB26-{cur.lastrowid:04d}", cur.lastrowid))
            log_activity(conn, admin["username"], "tambah_peserta", f"{data['full_name']} ({data['nim']})")
    except sqlite3.IntegrityError:
        raise ApiError(409, "NIM atau email sudah terdaftar.")
    r = conn.execute(PARTICIPANT_SELECT + " WHERE p.id = ?", (cur.lastrowid,)).fetchone()
    h.send_json(201, row_participant(r))


@route("PUT", r"/api/admin/participants/(?P<id>\d+)", auth="admin")
def api_participant_update(h, conn, admin, m, body):
    pid = int(m["id"])
    data = validate_participant(body)
    try:
        with tx(conn):
            if not conn.execute("SELECT 1 FROM participants WHERE id = ?", (pid,)).fetchone():
                raise ApiError(404, "Peserta tidak ditemukan.")
            for col, label in (("nim", "NIM"), ("email", "Email")):
                if conn.execute(f"SELECT 1 FROM participants WHERE lower({col}) = lower(?) AND id != ?", (data[col], pid)).fetchone():
                    raise ApiError(409, f"{label} sudah dipakai peserta lain.", {col: f"{label} sudah dipakai peserta lain."})
            conn.execute(
                "UPDATE participants SET full_name=?, nim=?, email=?, phone=?, program_studi=?, status=?, grad_year=?, "
                "interest=?, note=?, updated_at=? WHERE id=?",
                (data["full_name"], data["nim"], data["email"], data["phone"], data["program_studi"], data["status"],
                 data["grad_year"], data["interest"], data["note"], now_iso(), pid),
            )
            log_activity(conn, admin["username"], "ubah_peserta", f"#{pid} {data['full_name']}")
    except sqlite3.IntegrityError:
        raise ApiError(409, "NIM atau email sudah dipakai peserta lain.")
    r = conn.execute(PARTICIPANT_SELECT + " WHERE p.id = ?", (pid,)).fetchone()
    h.send_json(200, row_participant(r))


@route("DELETE", r"/api/admin/participants/(?P<id>\d+)", auth="admin")
def api_participant_delete(h, conn, admin, m, body):
    pid = int(m["id"])
    r = conn.execute("SELECT full_name, nim FROM participants WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise ApiError(404, "Peserta tidak ditemukan.")
    with tx(conn):
        conn.execute("DELETE FROM participants WHERE id = ?", (pid,))
        log_activity(conn, admin["username"], "hapus_peserta", f"#{pid} {r['full_name']} ({r['nim']})")
    h.send_json(200, {"ok": True})


# --------------------------------------------------------------------------- #
# Absensi
# --------------------------------------------------------------------------- #
def resolve_scan(conn, raw: str):
    """Terima isi QR (MEB26.<code>), URL tiket, kode mentah, No. Registrasi, atau NIM."""
    s = clean(raw, 300)
    if not s:
        return None
    mm = re.search(r"[?&]c=([A-Za-z0-9_\-]{8,40})", s)
    if mm:
        s = mm.group(1)
    if s.upper().startswith(QR_PREFIX):
        s = s[len(QR_PREFIX):]
    r = conn.execute(PARTICIPANT_SELECT + " WHERE p.code = ?", (s,)).fetchone()
    if not r:
        r = conn.execute(PARTICIPANT_SELECT + " WHERE upper(p.reg_no) = ? OR upper(p.nim) = ?", (s.upper(), s.upper())).fetchone()
    return r


@route("POST", "/api/admin/checkin", auth="staff")
def api_checkin(h, conn, admin, m, body):
    method = "manual" if body.get("method") == "manual" else "qr"
    r = resolve_scan(conn, body.get("code"))
    if not r:
        raise ApiError(404, "Kode tidak dikenali. Peserta tidak ditemukan.", extra={"result": "not_found"})
    ts = now_iso()
    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO attendance(participant_id, checked_in_at, checked_by, method) VALUES (?,?,?,?)",
            (r["id"], ts, admin["username"], method),
        )
        fresh = cur.rowcount == 1
        if fresh:
            log_activity(conn, admin["username"], "checkin", f"{r['reg_no']} {r['full_name']} ({method})")
    row = conn.execute(PARTICIPANT_SELECT + " WHERE p.id = ?", (r["id"],)).fetchone()
    h.send_json(200, {
        "result": "ok" if fresh else "duplicate",
        "participant": row_participant(row, include_code=False),
        "checked_by": row["checked_by"],
    })


@route("DELETE", r"/api/admin/attendance/(?P<pid>\d+)", auth="admin")
def api_attendance_undo(h, conn, admin, m, body):
    pid = int(m["pid"])
    with tx(conn):
        cur = conn.execute("DELETE FROM attendance WHERE participant_id = ?", (pid,))
        if cur.rowcount:
            log_activity(conn, admin["username"], "batal_checkin", f"peserta #{pid}")
    if not cur.rowcount:
        raise ApiError(404, "Peserta belum tercatat hadir.")
    h.send_json(200, {"ok": True})


# --------------------------------------------------------------------------- #
# Ekspor, backup, pengaturan, pengguna
# --------------------------------------------------------------------------- #
def csv_safe(v) -> str:
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def to_wita(ts: str | None) -> str:
    if not ts:
        return ""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(WITA).strftime("%Y-%m-%d %H:%M:%S")


@route("GET", "/api/admin/export.csv", auth="admin")
def api_export(h, conn, admin, m, body):
    where, args = build_filters(h.query)
    rows = conn.execute(f"{PARTICIPANT_SELECT}{where} ORDER BY p.id", args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["No. Registrasi", "Nama Lengkap", "NIM", "Email", "WhatsApp", "Program Studi", "Status",
                "Tahun Lulus", "Minat Karier", "Catatan", "Terdaftar (WITA)", "Hadir", "Waktu Hadir (WITA)", "Metode Check-in"])
    for r in rows:
        w.writerow([csv_safe(x) for x in (
            r["reg_no"], r["full_name"], r["nim"], r["email"], r["phone"], r["program_studi"],
            STATUS_LABEL.get(r["status"]), r["grad_year"], INTEREST_LABEL.get(r["interest"]), r["note"],
            to_wita(r["created_at"]), "Ya" if r["checked_in_at"] else "Tidak", to_wita(r["checked_in_at"]), r["method"] or "")])
    log_activity(conn, admin["username"], "ekspor_csv", f"{len(rows)} baris")
    fn = f"peserta-meb26-{datetime.now(WITA):%Y%m%d-%H%M}.csv"
    h.send_bytes(200, ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                 {"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-store"})


@route("GET", "/api/admin/backup", auth="admin")
def api_backup(h, conn, admin, m, body):
    data = conn.serialize()
    log_activity(conn, admin["username"], "backup_db", f"{len(data)} byte")
    fn = f"bootcamp-backup-{datetime.now(WITA):%Y%m%d-%H%M}.db"
    h.send_bytes(200, bytes(data), "application/octet-stream",
                 {"Content-Disposition": f'attachment; filename="{fn}"', "Cache-Control": "no-store"})


@route("GET", "/api/admin/settings", auth="admin")
def api_settings_get(h, conn, admin, m, body):
    h.send_json(200, get_settings(conn))


@route("PUT", "/api/admin/settings", auth="admin")
def api_settings_put(h, conn, admin, m, body):
    limits = {"event_title": 120, "event_subtitle": 240, "organizer": 140, "event_time": 60, "venue": 160,
              "contact_name": 80, "contact_phone": 25}
    new, errs = {}, {}
    for k, n in limits.items():
        if k in body:
            new[k] = clean(body[k], n)
    for k in ("event_date", "reg_deadline"):
        if k in body:
            v = clean(body[k], 10)
            try:
                if v:
                    date.fromisoformat(v)
                new[k] = v
            except ValueError:
                errs[k] = "Format tanggal tidak valid."
    if "quota" in body:
        try:
            q = int(body["quota"])
            if not 0 <= q <= 100000:
                raise ValueError
            new["quota"] = str(q)
        except (TypeError, ValueError):
            errs["quota"] = "Kuota harus angka 0-100000 (0 = tanpa batas)."
    if "reg_open" in body:
        new["reg_open"] = "1" if body["reg_open"] in (True, 1, "1", "true") else "0"
    if "event_title" in new and len(new["event_title"]) < 3:
        errs["event_title"] = "Judul acara wajib diisi."
    if errs:
        raise ApiError(422, "Periksa kembali isian pengaturan.", errs)
    with tx(conn):
        for k, v in new.items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
        log_activity(conn, admin["username"], "ubah_pengaturan", ", ".join(new))
    h.send_json(200, get_settings(conn))


@route("GET", "/api/admin/users", auth="admin")
def api_users(h, conn, admin, m, body):
    rows = conn.execute("SELECT id, username, role, created_at FROM admins ORDER BY id").fetchall()
    h.send_json(200, {"items": [dict(r) for r in rows], "me": admin["id"]})


@route("POST", "/api/admin/users", auth="admin")
def api_user_create(h, conn, admin, m, body):
    username = clean(body.get("username"), 30)
    pw = str(body.get("password") or "")
    role = body.get("role") if body.get("role") in ("admin", "petugas") else "petugas"
    errs = {}
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,30}", username):
        errs["username"] = "3-30 karakter: huruf, angka, titik, strip, garis bawah."
    if len(pw) < 8:
        errs["password"] = "Minimal 8 karakter."
    if errs:
        raise ApiError(422, "Periksa kembali isian.", errs)
    try:
        conn.execute("INSERT INTO admins(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                     (username, hash_password(pw), role, now_iso()))
    except sqlite3.IntegrityError:
        raise ApiError(409, "Username sudah dipakai.", {"username": "Username sudah dipakai."})
    log_activity(conn, admin["username"], "tambah_pengguna", f"{username} ({role})")
    h.send_json(201, {"ok": True})


@route("DELETE", r"/api/admin/users/(?P<id>\d+)", auth="admin")
def api_user_delete(h, conn, admin, m, body):
    uid = int(m["id"])
    if uid == admin["id"]:
        raise ApiError(409, "Anda tidak dapat menghapus akun yang sedang dipakai.")
    r = conn.execute("SELECT username FROM admins WHERE id = ?", (uid,)).fetchone()
    if not r:
        raise ApiError(404, "Pengguna tidak ditemukan.")
    conn.execute("DELETE FROM admins WHERE id = ?", (uid,))
    log_activity(conn, admin["username"], "hapus_pengguna", r["username"])
    h.send_json(200, {"ok": True})


@route("GET", "/api/admin/activity", auth="admin")
def api_activity(h, conn, admin, m, body):
    rows = conn.execute("SELECT ts, actor, action, detail FROM activity_log ORDER BY id DESC LIMIT 60").fetchall()
    h.send_json(200, {"items": [dict(r) for r in rows]})


# --------------------------------------------------------------------------- #
# Demo data (hanya untuk uji coba)
# --------------------------------------------------------------------------- #
DEMO_DOMAIN = "demo.example"


def seed_demo(n: int) -> int:
    first = ["Andi", "Muh.", "Ahmad", "Nur", "Siti", "Rizky", "Fadli", "Ayu", "Dewi", "Rahmat", "Ilham", "Aulia", "Reski", "Firman", "Nabila"]
    last = ["Saputra", "Pratama", "Rahman", "Hidayat", "Ramadhani", "Kurniawan", "Syam", "Basri", "Aksa", "Mallombasi", "Putri", "Wijaya"]
    prodi = ["D3 Teknik Mesin", "D4 Teknik Manufaktur", "D3 Teknik Mesin", "D4 Teknik Mesin Produksi"]
    conn = db()
    added = 0
    base = int(time.time() * 1000) % 1_000_000
    for i in range(n):
        name = f"{random.choice(first)} {random.choice(last)}"
        nim = f"44{22 + random.randint(0, 3)}{base + i:06d}"[:12]
        ts = (datetime.now(timezone.utc) - timedelta(days=random.randint(0, 12), minutes=random.randint(0, 1000))).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with tx(conn):
                cur = conn.execute(
                    "INSERT INTO participants(code, full_name, nim, email, phone, program_studi, status, grad_year, interest, note, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (secrets.token_urlsafe(12), name, nim, f"{nim}@{DEMO_DOMAIN}", f"0812{random.randint(10000000, 99999999)}",
                     random.choice(prodi), random.choice(list(STATUS_LABEL)), 2026, random.choice(list(INTEREST_LABEL)), "", ts, ts))
                conn.execute("UPDATE participants SET reg_no=? WHERE id=?", (f"MEB26-{cur.lastrowid:04d}", cur.lastrowid))
                if random.random() < 0.35:
                    conn.execute("INSERT INTO attendance(participant_id, checked_in_at, checked_by, method) VALUES (?,?,?,?)",
                                 (cur.lastrowid, now_iso(), "demo", random.choice(["qr", "qr", "manual"])))
            added += 1
        except sqlite3.IntegrityError:
            pass
    conn.close()
    return added


def purge_demo() -> int:
    conn = db()
    with tx(conn):
        n = conn.execute("DELETE FROM participants WHERE email LIKE ?", (f"%@{DEMO_DOMAIN}",)).rowcount
    conn.close()
    return n


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    is_https = False


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser(description="MEB Career Bootcamp 2026 - server pendaftaran & absensi QR")
    ap.add_argument("--host", default="0.0.0.0", help="alamat bind (default 0.0.0.0 agar bisa diakses via LAN)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--cert", help="file sertifikat TLS (PEM) untuk HTTPS")
    ap.add_argument("--key", help="file private key TLS (PEM)")
    ap.add_argument("--reset-admin", metavar="USERNAME", help="reset kata sandi akun admin lalu keluar")
    ap.add_argument("--seed-demo", type=int, metavar="N", help="tambah N peserta DEMO lalu keluar")
    ap.add_argument("--purge-demo", action="store_true", help="hapus semua peserta DEMO lalu keluar")
    args = ap.parse_args()

    creds = init_db()

    if args.reset_admin:
        pw = secrets.token_urlsafe(9)
        conn = db()
        cur = conn.execute("UPDATE admins SET password_hash=? WHERE username=?", (hash_password(pw), args.reset_admin))
        conn.execute("DELETE FROM sessions")
        conn.close()
        if not cur.rowcount:
            print(f"Akun '{args.reset_admin}' tidak ditemukan.")
            return 1
        print(f"Kata sandi baru untuk '{args.reset_admin}': {pw}")
        return 0
    if args.seed_demo:
        print(f"{seed_demo(args.seed_demo)} peserta demo ditambahkan (hapus dengan --purge-demo).")
        return 0
    if args.purge_demo:
        print(f"{purge_demo()} peserta demo dihapus.")
        return 0

    srv = Server((args.host, args.port), Handler)
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.is_https = True
    scheme = "https" if srv.is_https else "http"
    line = "=" * 64
    print(f"\n{line}\n  MEB Career Bootcamp 2026 - PNUP Teknik Mesin\n{line}")
    print(f"  Situs pendaftaran : {scheme}://localhost:{args.port}/")
    print(f"  Panel admin       : {scheme}://localhost:{args.port}/admin")
    print(f"  Akses jaringan    : {scheme}://{lan_ip()}:{args.port}/")
    print(f"  Database          : {DB_PATH}")
    if creds:
        print(f"{line}\n  AKUN ADMIN PERTAMA (catat sekarang, hanya tampil sekali):")
        print(f"     username : {creds[0]}\n     password : {creds[1]}")
        print("  Lupa sandi? jalankan: python server.py --reset-admin admin")
    print(f"{line}\n  Tekan Ctrl+C untuk berhenti.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
