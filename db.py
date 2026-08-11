"""Lớp truy cập dữ liệu SQLite cho bot nhắc chụp layout bưu cục.

Quy tắc nghiệp vụ:
- Mỗi BC phải gửi đủ SO_ANH_YEU_CAU ảnh/ngày (mặc định 2: layout + nhà vệ sinh).
- Mỗi BC thuộc một AM; AM bị phạt theo số BC không đạt.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "data/bot.db")


@contextmanager
def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS am (
                am_name  TEXT PRIMARY KEY,      -- tên AM, dùng làm khoá
                user_id  INTEGER,               -- telegram id để tag khi nhắc
                username TEXT
            );

            CREATE TABLE IF NOT EXISTS buu_cuc (
                code       TEXT PRIMARY KEY,    -- mã BC, vd 23009000
                name       TEXT NOT NULL DEFAULT '',
                am_name    TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS anh (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ngay       TEXT NOT NULL,       -- YYYY-MM-DD theo giờ VN
                code       TEXT NOT NULL,
                chat_id    INTEGER,
                message_id INTEGER,
                user_id    INTEGER,
                user_name  TEXT,
                file_id    TEXT,
                tre        INTEGER NOT NULL DEFAULT 0,  -- 1 = gửi sau giờ chốt
                created_at TEXT NOT NULL,
                UNIQUE (chat_id, message_id)    -- 1 tin nhắn ảnh chỉ đếm 1 lần
            );
            CREATE INDEX IF NOT EXISTS idx_anh_ngay_code ON anh (ngay, code);

            CREATE TABLE IF NOT EXISTS gan_nguoi (
                user_id   INTEGER PRIMARY KEY,  -- NVXL gán cố định với 1 BC
                user_name TEXT,
                code      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topic (
                am_name   TEXT PRIMARY KEY,     -- mỗi AM một topic riêng trong group
                thread_id INTEGER NOT NULL,
                ghi_chu   TEXT
            );
            """
        )
        # Nâng cấp DB cũ (đã tạo trước khi có cột 'tre') mà không mất dữ liệu.
        cot = {r["name"] for r in c.execute("PRAGMA table_info(anh)")}
        if "tre" not in cot:
            c.execute("ALTER TABLE anh ADD COLUMN tre INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------- topic -----

def set_topic(am_name: str, thread_id: int, ghi_chu: str = "") -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO topic (am_name, thread_id, ghi_chu) VALUES (?, ?, ?)
               ON CONFLICT(am_name) DO UPDATE SET thread_id = excluded.thread_id,
                                                  ghi_chu   = excluded.ghi_chu""",
            (am_name.strip(), thread_id, ghi_chu),
        )


def get_topic(am_name: str) -> int | None:
    with _conn() as c:
        row = c.execute("SELECT thread_id FROM topic WHERE am_name = ?",
                        (am_name.strip(),)).fetchone()
        return row["thread_id"] if row else None


def am_of_thread(thread_id: int) -> str | None:
    """Topic hiện tại là của AM nào — dùng để lệnh chạy trong topic chỉ trả dữ liệu AM đó."""
    with _conn() as c:
        row = c.execute("SELECT am_name FROM topic WHERE thread_id = ?", (thread_id,)).fetchone()
        return row["am_name"] if row else None


def del_topic(am_name: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM topic WHERE am_name = ?", (am_name.strip(),))
        return cur.rowcount > 0


def list_topic() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM topic ORDER BY am_name").fetchall()


def am_dang_hoat_dong() -> list[str]:
    """Các AM đang phụ trách ít nhất 1 BC active."""
    with _conn() as c:
        return [r["am_name"] for r in c.execute(
            """SELECT DISTINCT am_name FROM buu_cuc
               WHERE active = 1 AND am_name IS NOT NULL AND am_name != ''
               ORDER BY am_name"""
        )]


# ------------------------------------------------------------------- AM -----

def upsert_am(am_name: str, user_id: int | None = None, username: str | None = None) -> None:
    am_name = am_name.strip()
    if not am_name:
        return
    with _conn() as c:
        c.execute(
            """INSERT INTO am (am_name, user_id, username) VALUES (?, ?, ?)
               ON CONFLICT(am_name) DO UPDATE SET
                   user_id  = COALESCE(excluded.user_id, am.user_id),
                   username = COALESCE(excluded.username, am.username)""",
            (am_name, user_id, username),
        )


def get_am(am_name: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM am WHERE am_name = ?", (am_name.strip(),)).fetchone()


def am_by_username(username: str) -> str | None:
    """Tìm AM theo nick Telegram (không phân biệt hoa thường)."""
    u = (username or "").lstrip("@").strip().lower()
    if not u:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT am_name FROM am WHERE LOWER(username) = ?", (u,)
        ).fetchone()
        return row["am_name"] if row else None


def list_am() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM am ORDER BY am_name").fetchall()


# --------------------------------------------------------------- bưu cục ----

def upsert_bc(code: str, name: str = "", am_name: str = "") -> None:
    code = code.strip().upper()
    name, am_name = name.strip(), am_name.strip()
    if am_name:
        upsert_am(am_name)
    with _conn() as c:
        c.execute(
            """INSERT INTO buu_cuc (code, name, am_name, active, created_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(code) DO UPDATE SET
                   active  = 1,
                   name    = CASE WHEN excluded.name    != '' THEN excluded.name    ELSE buu_cuc.name    END,
                   am_name = CASE WHEN excluded.am_name != '' THEN excluded.am_name ELSE buu_cuc.am_name END""",
            (code, name, am_name or None, datetime.now().isoformat(timespec="seconds")),
        )


def remove_bc(code: str) -> bool:
    """Ngừng theo dõi một BC (giữ lịch sử, chỉ tắt active)."""
    with _conn() as c:
        cur = c.execute("UPDATE buu_cuc SET active = 0 WHERE code = ?", (code.strip().upper(),))
        return cur.rowcount > 0


def list_bc(active_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM buu_cuc"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY am_name, code"
    with _conn() as c:
        return c.execute(sql).fetchall()


def get_bc(code: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM buu_cuc WHERE code = ?", (code.strip().upper(),)).fetchone()


def bc_codes() -> list[str]:
    return [r["code"] for r in list_bc()]


# ------------------------------------------------------------ gán nhân sự ----

def bind_user(user_id: int, user_name: str, code: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO gan_nguoi (user_id, user_name, code) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET code = excluded.code,
                                                 user_name = excluded.user_name""",
            (user_id, user_name, code.strip().upper()),
        )


def unbind_user(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM gan_nguoi WHERE user_id = ?", (user_id,))


def get_binding(user_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT code FROM gan_nguoi WHERE user_id = ?", (user_id,)).fetchone()
        return row["code"] if row else None


def people_of(code: str) -> list[sqlite3.Row]:
    """NVXL được gán cho một BC — dùng để tag khi nhắc."""
    with _conn() as c:
        return c.execute(
            "SELECT user_id, user_name FROM gan_nguoi WHERE code = ?", (code.upper(),)
        ).fetchall()


# ---------------------------------------------------------------- nộp ảnh ----

def record_photo(ngay: str, code: str, chat_id: int, message_id: int,
                 user_id: int, user_name: str, file_id: str, tre: bool = False) -> int:
    """Ghi nhận 1 ảnh. Trả về tổng số ảnh BC đó đã gửi trong ngày (sau khi ghi)."""
    with _conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO anh
               (ngay, code, chat_id, message_id, user_id, user_name, file_id, tre, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ngay, code.upper(), chat_id, message_id, user_id, user_name, file_id,
             1 if tre else 0, datetime.now().isoformat(timespec="seconds")),
        )
        row = c.execute(
            "SELECT COUNT(*) AS n FROM anh WHERE ngay = ? AND code = ?", (ngay, code.upper())
        ).fetchone()
        return row["n"]


def photo_count(ngay: str, code: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM anh WHERE ngay = ? AND code = ?", (ngay, code.upper())
        ).fetchone()
        return row["n"]


def reset_day(ngay: str, code: str) -> int:
    """Xoá toàn bộ ảnh đã ghi nhận của 1 BC trong ngày (dùng khi gửi nhầm)."""
    with _conn() as c:
        cur = c.execute("DELETE FROM anh WHERE ngay = ? AND code = ?", (ngay, code.upper()))
        return cur.rowcount


def status(ngay: str) -> list[sqlite3.Row]:
    """Trạng thái toàn bộ BC active trong ngày.

    so_anh        = tổng số ảnh đã gửi
    so_anh_dung_han = số ảnh gửi trong khung giờ (không tính ảnh bổ sung sau giờ chốt)
    """
    with _conn() as c:
        return c.execute(
            """SELECT b.code, b.name, b.am_name,
                      (SELECT COUNT(*) FROM anh a
                       WHERE a.ngay = ? AND a.code = b.code) AS so_anh,
                      (SELECT COUNT(*) FROM anh a
                       WHERE a.ngay = ? AND a.code = b.code AND a.tre = 0) AS so_anh_dung_han
               FROM buu_cuc b
               WHERE b.active = 1
               ORDER BY b.am_name, b.code""",
            (ngay, ngay),
        ).fetchall()


def history(tu_ngay: str, den_ngay: str, so_anh_yeu_cau: int) -> list[sqlite3.Row]:
    """Số ngày ĐẠT (đủ ảnh) của từng BC trong khoảng ngày."""
    with _conn() as c:
        return c.execute(
            """SELECT b.code, b.name, b.am_name,
                      (SELECT COUNT(*) FROM (
                           SELECT a.ngay FROM anh a
                           WHERE a.code = b.code AND a.ngay BETWEEN ? AND ?
                           GROUP BY a.ngay HAVING COUNT(*) >= ?
                      )) AS so_ngay_dat
               FROM buu_cuc b
               WHERE b.active = 1
               ORDER BY so_ngay_dat ASC, b.code""",
            (tu_ngay, den_ngay, so_anh_yeu_cau),
        ).fetchall()


# -------------------------------------------------------------- tiện ích ----

def find_code_in_text(text: str) -> str | None:
    """Dò mã BC trong caption, ưu tiên mã dài nhất để tránh khớp nhầm tiền tố."""
    if not text:
        return None
    haystack = text.upper()
    for code in sorted(bc_codes(), key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", haystack):
            return code
    return None


DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")


def find_date_in_text(text: str) -> str | None:
    """Lấy ngày dd/mm/yyyy trong caption, trả về chuỗi YYYY-MM-DD."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return datetime(y, mth, d).strftime("%Y-%m-%d")
    except ValueError:
        return None
