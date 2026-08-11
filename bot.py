"""Bot Telegram nhắc & chốt danh sách BC chưa gửi hình layout hằng ngày.

Luồng:
  - BC gửi ảnh vào group kèm caption: "23009000 - (BGI) Đa Mai - 10/08/2026"
  - Bot dò mã BC trong caption -> ghi nhận ảnh cho ngày hôm nay
  - GIO_NHAC (20:30): nhắc các BC chưa gửi / gửi thiếu
  - GIO_CHOT (21:30): chốt danh sách, tổng hợp tiền phạt theo AM
"""

import html
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
import db  # noqa: E402  (db đọc DB_PATH từ env nên phải import sau load_dotenv)
import sync  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bc-bot")

# ----------------------------------------------------------------- config ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = ZoneInfo(os.getenv("TZ", "Asia/Ho_Chi_Minh"))
SO_ANH_YEU_CAU = int(os.getenv("SO_ANH_YEU_CAU", "2"))
MUC_PHAT = int(os.getenv("MUC_PHAT", "100000"))
GIO_NHAC = [t.strip() for t in os.getenv("GIO_NHAC", "20:30").split(",") if t.strip()]
GIO_CHOT = os.getenv("GIO_CHOT", "21:30").strip()
# Trước giờ này bot không ghi nhận ảnh — BC gửi sớm sẽ được nhắc gửi lại trong khung giờ.
GIO_NHAN_TU = os.getenv("GIO_NHAN_TU", "18:00").strip()
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", "0") or 0)
ALLOWED_CHAT_IDS = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
} or ({REPORT_CHAT_ID} if REPORT_CHAT_ID else set())
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# Nguồn danh sách BC – AM (Google Sheets hoặc URL trả về CSV/XLSX)
SHEET_URL = os.getenv("SHEET_URL", "").strip()
GIO_SYNC = os.getenv("GIO_SYNC", "07:00").strip()  # giờ tự kéo danh sách mỗi ngày

# Topic nhận bản tổng hợp toàn vùng. Để trống = topic General của group.
REPORT_THREAD_ID = int(os.getenv("REPORT_THREAD_ID", "0") or 0) or None

# Ngày bắt đầu áp dụng (YYYY-MM-DD). Trước ngày này bot vẫn đếm ảnh và trả lời lệnh,
# nhưng KHÔNG tự bắn nhắc/chốt vào group — tránh báo cáo sai trong lúc chạy thử.
NGAY_BAT_DAU = os.getenv("NGAY_BAT_DAU", "").strip()

# Trước ngày này là giai đoạn ân hạn: báo cáo chỉ liệt kê BC chưa gửi, KHÔNG tính phạt,
# và ảnh gửi sau giờ chốt vẫn được ghi nhận (có cảnh báo).
NGAY_AP_DUNG_PHAT = os.getenv("NGAY_AP_DUNG_PHAT", "2026-08-17").strip()

MAX_LEN = 3900  # giới hạn an toàn dưới mức 4096 ký tự của Telegram


# --------------------------------------------------------------- tiện ích ---
def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def vn_date(ngay: str) -> str:
    return datetime.strptime(ngay, "%Y-%m-%d").strftime("%d/%m/%Y")


def money(n: int) -> str:
    return f"{n:,.0f}".replace(",", ".") + "đ"


def esc(s) -> str:
    return html.escape(str(s or ""))


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def mention(user_id, name) -> str:
    if user_id:
        return f'<a href="tg://user?id={user_id}">{esc(name or "NV")}</a>'
    return esc(name or "")


def am_tag(am_name: str | None) -> str:
    """Chuỗi hiển thị AM kèm tag. Ưu tiên user_id (chắc chắn nhất), rồi tới @username."""
    if not am_name:
        return "chưa gán AM"
    row = db.get_am(am_name)
    if row and row["user_id"]:
        return mention(row["user_id"], am_name)
    if row and row["username"]:
        return f"{esc(am_name)} @{esc(row['username'])}"
    return esc(am_name)


def hhmm_sang_phut(s: str) -> int | None:
    """'18:00' → 1080. None nếu chuỗi không hợp lệ."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def dang_an_han() -> bool:
    """Còn trong giai đoạn nhắc nhở, chưa áp dụng phạt."""
    return bool(NGAY_AP_DUNG_PHAT) and today_str() < NGAY_AP_DUNG_PHAT


def cau_canh_bao_phat() -> str:
    return (f"📌 <b>Từ {vn_date(NGAY_AP_DUNG_PHAT)}, BC nào không gửi hình ảnh/gửi trễ, "
            f"AM sẽ bị phạt {money(MUC_PHAT)}/BC nhé ạ!</b>")


def trang_thai_gio() -> str:
    """Khung giờ nhận ảnh lúc này: 'chua_mo' | 'dang_mo' | 'da_dong'."""
    now = datetime.now(TZ)
    phut = now.hour * 60 + now.minute
    tu, den = hhmm_sang_phut(GIO_NHAN_TU), hhmm_sang_phut(GIO_CHOT)
    if tu is not None and phut < tu:
        return "chua_mo"
    if den is not None and phut >= den:
        return "da_dong"
    return "dang_mo"


def parse_hhmm(s: str) -> dtime | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    return dtime(hour=h, minute=mi, tzinfo=TZ)


async def send_long(bot, chat_id: int, text: str, thread_id: int | None = None) -> None:
    """Cắt tin nhắn dài theo dòng để không vượt giới hạn Telegram."""
    async def gui(noi_dung: str):
        kw = {"message_thread_id": thread_id} if thread_id else {}
        await bot.send_message(chat_id, noi_dung, parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True, **kw)

    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX_LEN:
            await gui(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await gui(buf)


def phan_loai(ngay: str, am_name: str | None = None):
    """Chia BC thành 4 nhóm: chưa gửi · gửi thiếu · gửi trễ · đạt.

    đạt   = đủ ảnh và đều gửi trong khung giờ
    trễ   = đủ số ảnh nhưng có ảnh bổ sung sau giờ chốt
    thiếu = có gửi nhưng chưa đủ số ảnh
    am_name != None → chỉ lấy BC của riêng AM đó (dùng khi bắn vào topic của AM).
    """
    chua, thieu, tre, du = [], [], [], []
    for r in db.status(ngay):
        if am_name is not None and (r["am_name"] or "") != am_name:
            continue
        if r["so_anh_dung_han"] >= SO_ANH_YEU_CAU:
            du.append(r)
        elif r["so_anh"] == 0:
            chua.append(r)
        elif r["so_anh"] >= SO_ANH_YEU_CAU:
            tre.append(r)
        else:
            thieu.append(r)
    return chua, thieu, tre, du


def dong_bc(i: int, r, kem_am: bool = True) -> str:
    """Một dòng BC. Trong topic riêng của AM thì bỏ phần AM cho gọn (kem_am=False)."""
    ten = f" — {esc(r['name'])}" if r["name"] else ""
    nv = db.people_of(r["code"])
    tag_nv = " " + " ".join(mention(p["user_id"], p["user_name"]) for p in nv) if nv else ""
    phan_am = f" · AM: {am_tag(r['am_name'])}" if kem_am else ""
    return f"{i}. <code>{esc(r['code'])}</code>{ten}{phan_am}{tag_nv}"


# ---------------------------------------------------------- xử lý ảnh gửi ---
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
        return

    user = update.effective_user
    caption = msg.caption or msg.text or ""
    ngay = today_str()

    # Album (media group): chỉ ảnh đầu có caption, các ảnh sau dùng lại mã đã dò.
    mg_cache = context.bot_data.setdefault("mg_cache", {})
    code = db.find_code_in_text(caption)
    if not code and msg.media_group_id:
        code = mg_cache.get(msg.media_group_id)
    if not code:
        code = db.get_binding(user.id)  # NVXL đã /gan thì không cần gõ mã

    if not code:
        if msg.media_group_id and mg_cache.get(("warned", msg.media_group_id)):
            return
        if msg.media_group_id:
            mg_cache[("warned", msg.media_group_id)] = True
        await msg.reply_text(
            "⚠️ Không nhận diện được mã BC.\n"
            "Cú pháp caption: <code>Mã BC - Tên BC - Ngày/Tháng/Năm</code>\n"
            "Ví dụ: <code>23009000 - (BGI) Đa Mai - 10/08/2026</code>\n"
            "Hoặc gõ <code>/gan &lt;mã BC&gt;</code> một lần để bot tự nhận ảnh của anh/chị.",
            parse_mode=ParseMode.HTML,
        )
        return

    if msg.media_group_id:
        if len(mg_cache) > 500:  # tránh phình bộ nhớ khi chạy dài ngày
            mg_cache.clear()
        mg_cache[msg.media_group_id] = code

    # Ngoài khung giờ. Mỗi BC chỉ nhận 1 tin cho mỗi tình huống trong ngày, tránh spam.
    tt = trang_thai_gio()
    da_bao = context.bot_data.setdefault("bao_ngoai_gio", set())
    if len(da_bao) > 2000:
        da_bao.clear()

    if tt == "chua_mo":  # gửi sớm thì không ghi nhận
        khoa = (ngay, code, tt)
        if khoa not in da_bao:
            da_bao.add(khoa)
            await msg.reply_text(
                f"⏳ <b>Chưa tới giờ nhận hình.</b>\n"
                f"Khung giờ gửi: <b>{GIO_NHAN_TU} – {GIO_CHOT}</b> mỗi ngày.\n"
                f"Ảnh này <b>chưa được tính</b> cho <code>{esc(code)}</code>, "
                f"đề nghị gửi lại sau {GIO_NHAN_TU}.",
                parse_mode=ParseMode.HTML,
            )
        return

    # Gửi trễ: vẫn ghi nhận nhưng đánh dấu để phân biệt khi tổng hợp.
    tre = tt == "da_dong"
    file_id = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else "")
    db.record_photo(ngay, code, chat.id, msg.message_id, user.id, user.full_name, file_id, tre)

    if tre:
        khoa = (ngay, code, tt)
        if khoa not in da_bao:
            da_bao.add(khoa)
            if dang_an_han():
                loi_nhac = (
                    f"📝 Bot tạm ghi nhận BC bổ sung hình ảnh, tuy nhiên, kể từ ngày "
                    f"<b>{vn_date(NGAY_AP_DUNG_PHAT)}</b>, nếu BC mình gửi trễ sẽ bị phạt "
                    f"<b>{money(MUC_PHAT)}/BC</b> nhé!"
                )
            else:
                loi_nhac = (
                    f"🕘 Bot đã ghi nhận BC bổ sung hình ảnh, nhưng <b>gửi sau {GIO_CHOT} "
                    f"vẫn tính là trễ</b> và bị phạt <b>{money(MUC_PHAT)}/BC</b>.\n"
                    f"Đề nghị gửi trong khung <b>{GIO_NHAN_TU} – {GIO_CHOT}</b> các ngày sau."
                )
            await msg.reply_text(loi_nhac, parse_mode=ParseMode.HTML)

    try:  # thả cảm xúc thay vì spam reply cho từng ảnh
        await context.bot.set_message_reaction(chat.id, msg.message_id, reaction="👍")
    except Exception:
        pass

    # Cảnh báo ngày trong caption không khớp hôm nay (ảnh cũ gửi lại)
    ngay_caption = db.find_date_in_text(caption)
    if ngay_caption and ngay_caption != ngay:
        await msg.reply_text(
            f"⚠️ Ngày trong caption là <b>{vn_date(ngay_caption)}</b> "
            f"nhưng hôm nay là <b>{vn_date(ngay)}</b>. Đề nghị kiểm tra lại.",
            parse_mode=ParseMode.HTML,
        )


# ------------------------------------------------------------- báo cáo -----
def _liet_ke(out: list, chua, thieu, tre, kem_am: bool, nhan_chua: str) -> None:
    """Ghép 3 nhóm chưa đạt vào bản báo cáo."""
    if chua:
        out.append(f"❌ <b>{nhan_chua} ({len(chua)})</b>")
        out += [dong_bc(i, r, kem_am) for i, r in enumerate(chua, 1)]
        out.append("")
    if thieu:
        out.append(f"⚠️ <b>GỬI THIẾU ({len(thieu)})</b>")
        out += [f"{dong_bc(i, r, kem_am)} — {r['so_anh']}/{SO_ANH_YEU_CAU}"
                for i, r in enumerate(thieu, 1)]
        out.append("")
    if tre:
        out.append(f"🕘 <b>GỬI TRỄ ({len(tre)})</b> — bổ sung sau {GIO_CHOT}")
        out += [dong_bc(i, r, kem_am) for i, r in enumerate(tre, 1)]
        out.append("")


def build_nhac(ngay: str, am_name: str | None = None) -> str:
    """Bản nhắc. am_name != None → chỉ nội dung của AM đó, tag AM đúng 1 lần ở đầu."""
    chua, thieu, tre, du = phan_loai(ngay, am_name)
    tong = len(chua) + len(thieu) + len(tre) + len(du)
    if tong == 0:
        return ""
    if not (chua or thieu or tre):
        return (f"🎉 <b>{vn_date(ngay)}</b> — {'toàn bộ' if am_name is None else 'cả'} "
                f"<b>{tong}</b> BC đã gửi đủ {SO_ANH_YEU_CAU} ảnh. Cảm ơn các anh/chị!")

    out = [f"⏰ <b>NHẮC GỬI HÌNH LAYOUT — {vn_date(ngay)}</b>"]
    if am_name:
        out.append(f"AM: {am_tag(am_name)}")
    out += [
        f"Khung giờ gửi: <b>{GIO_NHAN_TU} – {GIO_CHOT}</b> · "
        f"Yêu cầu: <b>{SO_ANH_YEU_CAU} ảnh</b> (layout + nhà vệ sinh, có timemark)",
        f"Đã đủ: <b>{len(du)}/{tong}</b>",
        "",
    ]
    _liet_ke(out, chua, thieu, tre, am_name is None, "CHƯA GỬI")
    out.append("👉 Đề nghị các BC hoàn tất trước hạn chót.")
    if dang_an_han():
        out.append("\n" + cau_canh_bao_phat())
    return "\n".join(out)


def build_chot(ngay: str, am_name: str | None = None) -> str:
    chua, thieu, tre, du = phan_loai(ngay, am_name)
    tong = len(chua) + len(thieu) + len(tre) + len(du)
    if tong == 0:
        return ""
    khong_dat = chua + thieu + tre
    an_han = dang_an_han()

    out = [f"🔴 <b>CHỐT DANH SÁCH {GIO_CHOT} — {vn_date(ngay)}</b>"]
    if am_name:
        out.append(f"AM: {am_tag(am_name)}")
    out.append(f"Tổng: <b>{tong}</b> BC · Đạt: <b>{len(du)}</b> · "
               f"Chưa đạt: <b>{len(khong_dat)}</b>")
    out.append("")

    if not khong_dat:
        out.append("🎉 <b>Hoàn thành 100% đúng hạn.</b>"
                   + ("" if an_han else " Không phát sinh phạt."))
        return "\n".join(out)

    _liet_ke(out, chua, thieu, tre, am_name is None, "KHÔNG GỬI")

    if an_han:  # giai đoạn nhắc nhở: chỉ nêu danh sách, chưa tính tiền
        out.append(cau_canh_bao_phat())
        return "\n".join(out)

    if am_name:  # trong topic riêng: chỉ nêu phạt của chính AM đó
        out.append(f"💰 <b>Phạt: {len(khong_dat)} BC × {money(MUC_PHAT)} = "
                   f"{money(len(khong_dat) * MUC_PHAT)}</b>")
        return "\n".join(out)

    theo_am: dict[str, int] = {}
    for r in khong_dat:
        theo_am[r["am_name"] or "Chưa gán AM"] = theo_am.get(r["am_name"] or "Chưa gán AM", 0) + 1
    out.append(f"💰 <b>TỔNG HỢP PHẠT AM</b> ({money(MUC_PHAT)}/BC/ngày)")
    for am, sl in sorted(theo_am.items(), key=lambda x: -x[1]):
        out.append(f"• {am_tag(am)}: {sl} BC = <b>{money(sl * MUC_PHAT)}</b>")
    out.append(f"<b>Tổng cộng: {money(len(khong_dat) * MUC_PHAT)}</b>")
    return "\n".join(out)


def build_tong_hop(ngay: str, la_chot: bool) -> str:
    """Bản tổng hợp toàn vùng cho topic chung — chỉ số liệu, KHÔNG tag ai."""
    chua, thieu, tre, du = phan_loai(ngay)
    tong = len(chua) + len(thieu) + len(tre) + len(du)
    khong_dat = chua + thieu + tre
    an_han = dang_an_han()
    tinh_tien = la_chot and not an_han

    tieu_de = (f"🔴 <b>CHỐT {GIO_CHOT}" if la_chot else f"⏰ <b>NHẮC {GIO_NHAC[0]}")
    out = [f"{tieu_de} — TOÀN VÙNG — {vn_date(ngay)}</b>",
           f"Tổng: <b>{tong}</b> BC · Đạt: <b>{len(du)}</b> · "
           f"Chưa đạt: <b>{len(khong_dat)}</b>"]
    if tinh_tien and khong_dat:
        out.append(f"💰 Tổng phạt: <b>{money(len(khong_dat) * MUC_PHAT)}</b>")
    if not khong_dat:
        out.append("🎉 Toàn vùng hoàn thành 100%.")
        return "\n".join(out)

    theo_am: dict[str, int] = {}
    for r in khong_dat:
        theo_am[r["am_name"] or "Chưa gán AM"] = theo_am.get(r["am_name"] or "Chưa gán AM", 0) + 1
    out.append("")
    for am, sl in sorted(theo_am.items(), key=lambda x: -x[1]):
        tien = f" = {money(sl * MUC_PHAT)}" if tinh_tien else ""
        out.append(f"• {esc(am)}: {sl} BC{tien}")  # không tag, tránh trùng thông báo
    out.append("\n<i>Chi tiết từng BC đã gửi vào topic của mỗi AM.</i>")
    if an_han:
        out.append("\n" + cau_canh_bao_phat())
    return "\n".join(out)


async def _bao_cao(context: ContextTypes.DEFAULT_TYPE, la_chot: bool) -> None:
    """Bắn báo cáo vào topic riêng của từng AM, rồi tổng hợp vào topic chung."""
    ngay = today_str()
    build = build_chot if la_chot else build_nhac
    khong_co_topic = []

    for am_name in db.am_dang_hoat_dong():
        thread_id = db.get_topic(am_name)
        if not thread_id:
            khong_co_topic.append(am_name)
            continue
        noi_dung = build(ngay, am_name)
        if not noi_dung:
            continue
        try:
            await send_long(context.bot, REPORT_CHAT_ID, noi_dung, thread_id)
        except Exception:
            log.exception("Không gửi được vào topic %s (AM %s)", thread_id, am_name)
            khong_co_topic.append(am_name)

    await send_long(context.bot, REPORT_CHAT_ID, build_tong_hop(ngay, la_chot),
                    REPORT_THREAD_ID)

    if khong_co_topic:
        await send_long(
            context.bot, REPORT_CHAT_ID,
            "⚠️ <b>Chưa đăng ký topic, không bắn riêng được</b>\n"
            + "\n".join(f"• {am_tag(a)}" for a in khong_co_topic)
            + "\n\nVào topic của AM đó gõ <code>/dangkytopic &lt;tên AM&gt;</code>.",
            REPORT_THREAD_ID,
        )


async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tự nhận diện topic: AM nào nhắn trong topic chưa gắn thì gắn topic đó cho AM ấy.

    Chạy ở group=1 nên không cản các handler khác. Gắn sai thì gõ /xoatopic để gỡ.
    """
    msg = update.effective_message
    chat = update.effective_chat
    tid = msg.message_thread_id if msg else None
    if not tid or (ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS):
        return
    if db.am_of_thread(tid):
        return

    am_name = db.am_by_username(update.effective_user.username or "")
    if not am_name or db.get_topic(am_name):
        return

    db.set_topic(am_name, tid)
    so_bc = len([r for r in db.list_bc() if r["am_name"] == am_name])
    log.info("Tự gắn topic %s cho AM %s", tid, am_name)
    await msg.reply_text(
        f"📌 Đã tự nhận topic này là của AM <b>{esc(am_name)}</b> ({so_bc} BC).\n"
        f"Bản nhắc {GIO_NHAC[0]} và bản chốt {GIO_CHOT} sẽ bắn vào đây.\n"
        f"<i>Nếu sai, gõ /xoatopic để gỡ.</i>",
        parse_mode=ParseMode.HTML,
    )


def chua_toi_ngay_chay() -> bool:
    if NGAY_BAT_DAU and today_str() < NGAY_BAT_DAU:
        log.info("Chưa tới NGAY_BAT_DAU=%s, bỏ qua báo cáo tự động hôm nay.", NGAY_BAT_DAU)
        return True
    return False


async def job_nhac(context: ContextTypes.DEFAULT_TYPE) -> None:
    if chua_toi_ngay_chay():
        return
    await _bao_cao(context, la_chot=False)


async def job_chot(context: ContextTypes.DEFAULT_TYPE) -> None:
    if chua_toi_ngay_chay():
        return
    await _bao_cao(context, la_chot=True)


# ------------------------------------------------------------- lệnh bot ----
HELP = """<b>BOT NHẮC CHỤP LAYOUT BƯU CỤC</b>

<b>Gửi ảnh</b> — caption theo cú pháp:
<code>Mã BC - Tên BC - Ngày/Tháng/Năm</code>
Ví dụ: <code>23009000 - (BGI) Đa Mai - 11/08/2026</code>
Mỗi BC gửi <b>{n} ảnh</b>/ngày (layout + nhà vệ sinh, có timemark).
⏰ Khung giờ nhận: <b>{tu} – {chot}</b>. Gửi ngoài khung giờ bot không tính.

<b>Cho nhân viên</b>
/gan &lt;mã BC&gt; — gán mình với 1 BC, sau đó gửi ảnh không cần gõ mã
/huygan — bỏ gán
/thieu — xem BC chưa gửi / gửi thiếu hôm nay
/da — xem BC đã đủ hôm nay
/id — xem chat id &amp; user id

<b>Topic theo AM</b>
/dangkytopic &lt;tên AM&gt; — gõ BÊN TRONG topic của AM để gắn topic đó cho AM ấy
/dstopic — xem AM nào đã/chưa có topic
/xoatopic — gỡ topic hiện tại
/dsam — danh sách AM và số BC phụ trách

<b>Cho quản lý</b>
/sync — kéo lại danh sách BC–AM từ file Google Sheets đã cấu hình
<i>hoặc</i> gửi file .csv/.xlsx vào chat kèm caption <code>/capnhat</code>
/thembc &lt;mã&gt; | &lt;tên&gt; | &lt;AM&gt;
/importbc — dán danh sách nhiều dòng, mỗi dòng: <code>mã | tên | AM</code>
/xoabc &lt;mã&gt;
/dsbc — danh sách BC đang theo dõi
/setam &lt;tên AM&gt; — chỉ cần khi AM chưa có nick Telegram trong sheet
/reset &lt;mã&gt; — xoá ảnh đã ghi nhận hôm nay của 1 BC
/nhac — gửi bản nhắc ngay
/chot — gửi bản chốt ngay
/tuan — thống kê 7 ngày gần nhất
/lich — xem cấu hình giờ"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP.format(n=SO_ANH_YEU_CAU, tu=GIO_NHAN_TU, chot=GIO_CHOT),
        parse_mode=ParseMode.HTML,
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    await update.effective_message.reply_text(
        f"<b>chat_id:</b> <code>{chat.id}</code>\n"
        f"<b>user_id:</b> <code>{user.id}</code> ({esc(user.full_name)})",
        parse_mode=ParseMode.HTML,
    )


async def cmd_thembc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    raw = " ".join(context.args)
    if not raw:
        return await msg.reply_text("Cú pháp: /thembc 23009000 | (BGI) Đa Mai | Nguyễn Văn A")
    parts = [p.strip() for p in raw.split("|")]
    code = parts[0].upper()
    db.upsert_bc(code, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
    await msg.reply_text(f"✅ Đã thêm/cập nhật BC {code}.")


async def cmd_importbc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    body = (msg.text or "").split("\n")[1:]
    if not body:
        return await msg.reply_text(
            "Dán danh sách ngay dưới lệnh, mỗi dòng một BC:\n"
            "/importbc\n23009000 | (BGI) Đa Mai | Nguyễn Văn A\n23009001 | (BGI) Lạng Giang | Nguyễn Văn A"
        )
    n = 0
    for line in body:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[|\t;]", line)]
        if not parts[0]:
            continue
        db.upsert_bc(parts[0].upper(), parts[1] if len(parts) > 1 else "",
                     parts[2] if len(parts) > 2 else "")
        n += 1
    await msg.reply_text(f"✅ Đã nạp {n} BC. Xem lại bằng /dsbc")


async def cmd_xoabc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    if not context.args:
        return await msg.reply_text("Cú pháp: /xoabc 23009000")
    ok = db.remove_bc(context.args[0])
    await msg.reply_text("✅ Đã ngừng theo dõi." if ok else "Không tìm thấy mã BC này.")


async def cmd_dsbc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.list_bc()
    if not rows:
        return await update.effective_message.reply_text(
            "Chưa có BC nào. Dùng /importbc để nạp danh sách."
        )
    out = [f"<b>DANH SÁCH BC THEO DÕI ({len(rows)})</b>"]
    am_hien_tai = object()
    for r in rows:
        if r["am_name"] != am_hien_tai:
            am_hien_tai = r["am_name"]
            out.append(f"\n<b>AM: {esc(am_hien_tai or 'Chưa gán')}</b>")
        out.append(f"• <code>{esc(r['code'])}</code> {esc(r['name'])}")
    await send_long(context.bot, update.effective_chat.id, "\n".join(out))


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kéo danh sách BC – AM từ SHEET_URL."""
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    url = " ".join(context.args).strip() or SHEET_URL
    if not url:
        return await msg.reply_text(
            "Chưa cấu hình nguồn danh sách.\n"
            "• Cách 1: đặt biến môi trường SHEET_URL = link Google Sheets\n"
            "• Cách 2: gõ /sync <link>\n"
            "• Cách 3: gửi thẳng file .csv/.xlsx vào chat kèm caption /capnhat"
        )
    await msg.reply_text("⏳ Đang tải danh sách…")
    try:
        records = await sync.from_url(url)
    except Exception as e:
        log.exception("Lỗi tải sheet")
        return await msg.reply_text(f"❌ Không tải được file: {esc(e)}\n"
                                    f"Kiểm tra link đã chia sẻ ở chế độ 'Anyone with the link' chưa.",
                                    parse_mode=ParseMode.HTML)
    await send_long(context.bot, update.effective_chat.id, sync.tom_tat(sync.apply(records)))


async def on_document_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nhận file .csv/.xlsx gửi kèm caption /capnhat."""
    msg = update.effective_message
    if not (msg.caption or "").strip().lower().startswith("/capnhat"):
        return
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên được cập nhật danh sách.")

    doc = msg.document
    try:
        f = await context.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
        records = (sync.from_xlsx_bytes(data) if doc.file_name.lower().endswith((".xlsx", ".xlsm"))
                   else sync.from_csv_bytes(data))
    except Exception as e:
        log.exception("Lỗi đọc file đính kèm")
        return await msg.reply_text(f"❌ Không đọc được file: {esc(e)}", parse_mode=ParseMode.HTML)
    await send_long(context.bot, update.effective_chat.id, sync.tom_tat(sync.apply(records)))


async def job_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not SHEET_URL:
        return
    try:
        kq = sync.apply(await sync.from_url(SHEET_URL))
    except Exception:
        log.exception("Đồng bộ tự động thất bại")
        return
    # Chỉ báo vào group khi thực sự có thay đổi, tránh làm phiền mỗi sáng.
    if kq.get("loi") or kq.get("moi") or kq.get("doi_am") or kq.get("ngung"):
        await send_long(context.bot, REPORT_CHAT_ID, sync.tom_tat(kq))


async def cmd_setam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not context.args:
        return await msg.reply_text("Cú pháp: /setam Nguyễn Văn A  (đúng tên AM đã nhập ở /importbc)")
    am_name = " ".join(context.args).strip()
    db.upsert_am(am_name, user.id, user.username)
    await msg.reply_text(f"✅ Đã liên kết AM <b>{esc(am_name)}</b> với tài khoản của anh/chị. "
                         f"Bot sẽ tag đúng người khi nhắc.", parse_mode=ParseMode.HTML)


def tim_am(ten: str) -> str | None:
    """Khớp tên AM kiểu dễ tính: bỏ dấu, không phân biệt hoa thường, cho phép khớp một phần."""
    ds = db.am_dang_hoat_dong()
    goc = sync._bo_dau(ten)
    for a in ds:
        if sync._bo_dau(a) == goc:
            return a
    khop = [a for a in ds if goc in sync._bo_dau(a) or sync._bo_dau(a) in goc]
    return khop[0] if len(khop) == 1 else None


def am_cua_topic(update: Update) -> str | None:
    """AM ứng với topic mà lệnh đang được gõ (None nếu ở General hoặc topic chưa đăng ký)."""
    tid = update.effective_message.message_thread_id
    return db.am_of_thread(tid) if tid else None


async def cmd_dangkytopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gõ trong topic của AM nào thì gắn topic đó cho AM ấy."""
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    tid = msg.message_thread_id
    if not tid:
        return await msg.reply_text("Lệnh này phải gõ BÊN TRONG topic của AM, không phải General.")
    if not context.args:
        return await msg.reply_text("Cú pháp: /dangkytopic Bùi Xuân Quang")

    ten = " ".join(context.args).strip()
    am_name = tim_am(ten)
    if not am_name:
        return await msg.reply_text(
            f"Không tìm ra AM khớp với “{esc(ten)}”. Gõ /dsam để xem danh sách tên chuẩn.",
            parse_mode=ParseMode.HTML)
    db.set_topic(am_name, tid)
    await msg.reply_text(
        f"✅ Đã gắn topic này cho AM <b>{esc(am_name)}</b>.\n"
        f"Từ nay bản nhắc {GIO_NHAC[0]} và bản chốt {GIO_CHOT} của "
        f"{len([r for r in db.list_bc() if r['am_name'] == am_name])} BC sẽ bắn vào đây.",
        parse_mode=ParseMode.HTML)


async def cmd_xoatopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    am_name = am_cua_topic(update)
    if not am_name:
        return await msg.reply_text("Topic này chưa gắn với AM nào.")
    db.del_topic(am_name)
    await msg.reply_text(f"✅ Đã gỡ topic khỏi AM {esc(am_name)}.", parse_mode=ParseMode.HTML)


async def cmd_dstopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    da_gan = {r["am_name"]: r["thread_id"] for r in db.list_topic()}
    ds = db.am_dang_hoat_dong()
    thieu = [a for a in ds if a not in da_gan]
    out = [f"<b>TOPIC THEO AM ({len(da_gan)}/{len(ds)})</b>", ""]
    for a in ds:
        if a in da_gan:
            so_bc = len([r for r in db.list_bc() if r["am_name"] == a])
            out.append(f"✅ {esc(a)} — topic <code>{da_gan[a]}</code> · {so_bc} BC")
    if thieu:
        out.append(f"\n❌ <b>Chưa đăng ký ({len(thieu)})</b>")
        out += [f"• {esc(a)}" for a in thieu]
        out.append("\nVào topic của AM đó gõ <code>/dangkytopic &lt;tên AM&gt;</code>.")
    await send_long(context.bot, update.effective_chat.id, "\n".join(out),
                    update.effective_message.message_thread_id)


async def cmd_dsam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ds = db.am_dang_hoat_dong()
    out = [f"<b>DANH SÁCH AM ({len(ds)})</b>", ""]
    for a in ds:
        so_bc = len([r for r in db.list_bc() if r["am_name"] == a])
        out.append(f"• {am_tag(a)} — {so_bc} BC")
    await send_long(context.bot, update.effective_chat.id, "\n".join(out),
                    update.effective_message.message_thread_id)


async def cmd_gan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not context.args:
        return await msg.reply_text("Cú pháp: /gan 23009000")
    code = context.args[0].strip().upper()
    if not db.get_bc(code):
        return await msg.reply_text(f"Mã BC {code} chưa có trong danh sách theo dõi.")
    db.bind_user(user.id, user.full_name, code)
    await msg.reply_text(f"✅ Đã gán anh/chị với BC <code>{esc(code)}</code>. "
                         f"Từ giờ gửi ảnh không cần gõ mã.", parse_mode=ParseMode.HTML)


async def cmd_huygan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.unbind_user(update.effective_user.id)
    await update.effective_message.reply_text("✅ Đã bỏ gán.")


async def cmd_thieu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Gõ trong topic của AM → chỉ dữ liệu AM đó. Gõ ở General → toàn vùng.
    am_name = am_cua_topic(update)
    await send_long(context.bot, update.effective_chat.id,
                    build_nhac(today_str(), am_name) or "Không có BC nào thuộc topic này.",
                    update.effective_message.message_thread_id)


async def cmd_da(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ngay = today_str()
    am_name = am_cua_topic(update)
    _, _, _, du = phan_loai(ngay, am_name)
    if not du:
        return await update.effective_message.reply_text("Chưa có BC nào gửi đủ ảnh hôm nay.")
    out = [f"✅ <b>ĐÃ ĐỦ ẢNH — {vn_date(ngay)} ({len(du)} BC)</b>"]
    out += [f"{i}. <code>{esc(r['code'])}</code> {esc(r['name'])}" for i, r in enumerate(du, 1)]
    await send_long(context.bot, update.effective_chat.id, "\n".join(out),
                    update.effective_message.message_thread_id)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return await msg.reply_text("Chỉ quản trị viên dùng được lệnh này.")
    if not context.args:
        return await msg.reply_text("Cú pháp: /reset 23009000")
    n = db.reset_day(today_str(), context.args[0])
    await msg.reply_text(f"✅ Đã xoá {n} ảnh ghi nhận hôm nay của {context.args[0].upper()}.")


async def cmd_nhac(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ở General: bắn full vào từng topic như job thật. Trong topic: chỉ xem của AM đó."""
    am_name = am_cua_topic(update)
    if am_name:
        return await send_long(context.bot, update.effective_chat.id,
                               build_nhac(today_str(), am_name),
                               update.effective_message.message_thread_id)
    if not is_admin(update.effective_user.id):
        return await update.effective_message.reply_text("Chỉ quản trị viên bắn nhắc toàn vùng.")
    await _bao_cao(context, la_chot=False)


async def cmd_chot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    am_name = am_cua_topic(update)
    if am_name:
        return await send_long(context.bot, update.effective_chat.id,
                               build_chot(today_str(), am_name),
                               update.effective_message.message_thread_id)
    if not is_admin(update.effective_user.id):
        return await update.effective_message.reply_text("Chỉ quản trị viên chốt toàn vùng.")
    await _bao_cao(context, la_chot=True)


async def cmd_tuan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    den = datetime.now(TZ).date()  # theo giờ VN, không theo giờ server
    tu = den - timedelta(days=6)
    rows = db.history(tu.strftime("%Y-%m-%d"), den.strftime("%Y-%m-%d"), SO_ANH_YEU_CAU)
    out = [f"📊 <b>THỐNG KÊ 7 NGÀY ({tu.strftime('%d/%m')} – {den.strftime('%d/%m')})</b>",
           "Số ngày đạt đủ ảnh / 7:", ""]
    for r in rows:
        icon = "🟢" if r["so_ngay_dat"] >= 7 else ("🟡" if r["so_ngay_dat"] >= 5 else "🔴")
        out.append(f"{icon} <code>{esc(r['code'])}</code> {esc(r['name'])} — "
                   f"{r['so_ngay_dat']}/7 · AM: {esc(r['am_name'] or '-')}")
    await send_long(context.bot, update.effective_chat.id, "\n".join(out))


async def cmd_lich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"⏱ <b>Cấu hình hiện tại</b>\n"
        f"Múi giờ: <code>{TZ}</code>\n"
        f"Khung giờ nhận ảnh: <b>{GIO_NHAN_TU} – {GIO_CHOT}</b> "
        f"{ {'chua_mo': '(chưa mở)', 'dang_mo': '(đang mở)', 'da_dong': '(đã đóng)'}[trang_thai_gio()] }\n"
        f"Giờ nhắc: <b>{', '.join(GIO_NHAC)}</b>\n"
        f"Giờ chốt: <b>{GIO_CHOT}</b>\n"
        f"Số ảnh yêu cầu: <b>{SO_ANH_YEU_CAU}</b>\n"
        f"Mức phạt: <b>{money(MUC_PHAT)}</b>/BC/ngày — "
        f"{'áp dụng từ ' + vn_date(NGAY_AP_DUNG_PHAT) if dang_an_han() else 'đang áp dụng'}\n"
        f"Group báo cáo: <code>{REPORT_CHAT_ID}</code>\n"
        f"Đồng bộ danh sách: {('mỗi ngày ' + GIO_SYNC) if SHEET_URL else 'chưa cấu hình SHEET_URL'}\n"
        + (f"⏸ <b>Chưa áp dụng</b> — báo cáo tự động bắt đầu từ {vn_date(NGAY_BAT_DAU)}"
           if chua_toi_ngay_chay() else "▶️ Đang áp dụng"),
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------- khởi động ---
async def post_init(app: Application) -> None:
    db.init_db()
    jq = app.job_queue
    for hhmm in GIO_NHAC:
        t = parse_hhmm(hhmm)
        if t:
            jq.run_daily(job_nhac, time=t, name=f"nhac-{hhmm}")
            log.info("Đã lên lịch nhắc lúc %s", hhmm)
        else:
            log.warning("GIO_NHAC không hợp lệ: %s", hhmm)
    t = parse_hhmm(GIO_CHOT)
    if t:
        jq.run_daily(job_chot, time=t, name="chot")
        log.info("Đã lên lịch chốt lúc %s", GIO_CHOT)
    if SHEET_URL:
        t = parse_hhmm(GIO_SYNC)
        if t:
            jq.run_daily(job_sync, time=t, name="sync")
            log.info("Đã lên lịch đồng bộ danh sách BC lúc %s", GIO_SYNC)
        jq.run_once(job_sync, when=5, name="sync-khoi-dong")  # kéo ngay khi bot vừa lên
    await app.bot.set_my_commands([
        BotCommand("thieu", "BC chưa gửi / gửi thiếu hôm nay"),
        BotCommand("da", "BC đã gửi đủ hôm nay"),
        BotCommand("gan", "Gán mình với 1 mã BC"),
        BotCommand("dangkytopic", "Gắn topic hiện tại cho 1 AM"),
        BotCommand("dstopic", "AM nào đã/chưa có topic"),
        BotCommand("dsbc", "Danh sách BC theo dõi"),
        BotCommand("tuan", "Thống kê 7 ngày"),
        BotCommand("chot", "Chốt danh sách ngay"),
        BotCommand("id", "Xem chat id / user id"),
        BotCommand("start", "Hướng dẫn sử dụng"),
    ])


def start_health_server() -> None:
    """Render free tier bắt buộc mở cổng HTTP; Railway thì bỏ qua."""
    port = os.getenv("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", int(port)), H).serve_forever(), daemon=True
    ).start()
    log.info("Health server chạy ở cổng %s", port)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Thiếu BOT_TOKEN. Tạo file .env từ .env.example.")
    if not REPORT_CHAT_ID:
        log.warning("Chưa đặt REPORT_CHAT_ID — bot sẽ không tự gửi nhắc/chốt. "
                    "Thêm bot vào group rồi gõ /id để lấy chat id.")
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("thembc", cmd_thembc))
    app.add_handler(CommandHandler("importbc", cmd_importbc))
    app.add_handler(CommandHandler("xoabc", cmd_xoabc))
    app.add_handler(CommandHandler("dsbc", cmd_dsbc))
    app.add_handler(CommandHandler("setam", cmd_setam))
    app.add_handler(CommandHandler("gan", cmd_gan))
    app.add_handler(CommandHandler("huygan", cmd_huygan))
    app.add_handler(CommandHandler("thieu", cmd_thieu))
    app.add_handler(CommandHandler("da", cmd_da))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("nhac", cmd_nhac))
    app.add_handler(CommandHandler("chot", cmd_chot))
    app.add_handler(CommandHandler("tuan", cmd_tuan))
    app.add_handler(CommandHandler("lich", cmd_lich))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("dangkytopic", cmd_dangkytopic))
    app.add_handler(CommandHandler("xoatopic", cmd_xoatopic))
    app.add_handler(CommandHandler("dstopic", cmd_dstopic))
    app.add_handler(CommandHandler("dsam", cmd_dsam))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("csv") | filters.Document.FileExtension("xlsx")
        | filters.Document.FileExtension("xlsm"),
        on_document_sync,
    ))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    # group=1: chạy sau, chỉ để tự nhận diện topic của AM, không cản handler nào
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_any_message), group=1)

    log.info("Bot đang chạy…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
