"""Soi chất lượng ảnh BC gửi: ảnh mờ và ảnh chụp sai yêu cầu.

Hai lớp kiểm tra, chạy nối tiếp:

  1. ĐỘ NÉT — đo cục bộ, miễn phí, không cần mạng. Dùng phương sai Laplacian:
     ảnh nét có nhiều biên sắc → phương sai cao; ảnh rung/mất nét → phương sai thấp.
     Ngưỡng chỉnh bằng NGUONG_NET.

  2. NỘI DUNG — gọi Claude vision phân loại ảnh thuộc nhóm nào trong 3 ảnh bắt buộc
     và có đạt yêu cầu chụp không. Cần ANTHROPIC_API_KEY; không có key thì bot chỉ
     chạy lớp 1 và bỏ qua lớp này (không chặn luồng cũ).

Ba ảnh bắt buộc mỗi BC:
  1. Ngoại quan — chụp từ ngoài vào, thấy CẢ biển GHN VÀ phần bên trong/mặt tiền BC
  2. Layout    — bên trong BC, thể hiện tổng thể khu vực, bố trí gọn gàng
  3. Nhà vệ sinh
"""

import asyncio
import io
import json
import logging
import os

log = logging.getLogger("soi-anh")

# Dưới ngưỡng này coi là ảnh mờ. Đo trên ảnh xám đã co về chiều rộng chuẩn nên
# giá trị không phụ thuộc kích thước ảnh gốc.
NGUONG_NET = float(os.getenv("NGUONG_NET", "60"))
RONG_CHUAN = 640          # chiều rộng chuẩn hoá khi đo độ nét
RONG_GUI_API = 800        # co ảnh trước khi gửi API cho rẻ và nhanh

MODEL_SOI_ANH = os.getenv("MODEL_SOI_ANH", "claude-opus-5").strip()
BAT_SOI_NOI_DUNG = os.getenv("BAT_SOI_NOI_DUNG", "1").strip() not in ("0", "", "false")

TEN_LOAI = {
    "ngoai_quan": "ngoại quan bưu cục",
    "layout": "layout bên trong",
    "wc": "nhà vệ sinh",
    "khong_ro": "không xác định",
}

HUONG_DAN = {
    "ngoai_quan": ("Đứng lùi ra ngoài cửa, chụp thẳng vào bưu cục sao cho <b>thấy cả "
                   "biển GHN phía trên và khoảng không gian bên trong</b> qua cửa."),
    "layout": ("Đứng ở cửa chụp vào trong, lấy được tổng thể khu vực làm việc, "
               "hàng hoá xếp gọn gàng."),
    "wc": "Chụp rõ khu vực nhà vệ sinh, đủ sáng, sạch sẽ.",
    "khong_ro": ("Chụp lại đúng 1 trong 3 ảnh yêu cầu: ngoại quan · layout bên trong · "
                 "nhà vệ sinh."),
}

SYSTEM = """Bạn là người kiểm tra ảnh chụp bưu cục của công ty chuyển phát GHN.

Mỗi bưu cục phải gửi đúng 3 ảnh mỗi ngày:

1. "ngoai_quan" — chụp từ NGOÀI vào bưu cục. ĐẠT khi thấy ĐƯỢC CẢ HAI: biển hiệu
   GHN/GiaoHangNhanh, VÀ mặt tiền cùng phần không gian bên trong nhìn qua cửa.
   KHÔNG ĐẠT nếu ảnh chỉ chụp mỗi tấm biển (thường là chụp chếch lên trời, nền là
   bầu trời, không thấy cửa hay bên trong), hoặc chỉ thấy cửa mà không thấy biển.

2. "layout" — chụp BÊN TRONG bưu cục, thể hiện tổng thể khu vực làm việc và hàng hoá.
   ĐẠT khi thấy được không gian bên trong ở diện rộng. KHÔNG ĐẠT nếu chỉ chụp cận
   một góc nhỏ, một kiện hàng, hay một bức tường trống.

3. "wc" — nhà vệ sinh của bưu cục. ĐẠT khi thấy rõ khu vực vệ sinh.

Đặt "loai" = "khong_ro" nếu ảnh không thuộc nhóm nào (ảnh chụp màn hình, ảnh chân
dung, ảnh giấy tờ, ảnh ngoài đường…).

Đặt "mo" = true khi ảnh rung, nhoè, mất nét, hoặc tối tới mức không nhìn rõ chi tiết.
Ảnh mờ luôn có "dat" = false.

"ly_do": một câu tiếng Việt ngắn gọn, lịch sự, nói rõ ảnh thiếu gì — chỉ điền khi
"dat" = false, còn lại để chuỗi rỗng. Không nhắc tới việc bạn là AI.

Ảnh có dòng timestamp/toạ độ chèn sẵn là bình thường, không phải lỗi."""

SCHEMA = {
    "type": "object",
    "properties": {
        "loai": {"type": "string", "enum": ["ngoai_quan", "layout", "wc", "khong_ro"]},
        "dat": {"type": "boolean"},
        "mo": {"type": "boolean"},
        "ly_do": {"type": "string"},
    },
    "required": ["loai", "dat", "mo", "ly_do"],
    "additionalProperties": False,
}


# ------------------------------------------------------------- độ nét -------
def _xam_chuan(data: bytes):
    """Ảnh → mảng numpy xám, co về RONG_CHUAN để ngưỡng không lệ thuộc kích thước."""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.convert("L")
    if img.width > RONG_CHUAN:
        img = img.resize((RONG_CHUAN, max(1, round(img.height * RONG_CHUAN / img.width))))
    return np.asarray(img, dtype="float32")


def do_net(data: bytes) -> float | None:
    """Phương sai Laplacian. Càng cao càng nét. None nếu không đọc được ảnh."""
    try:
        import numpy as np

        a = _xam_chuan(data)
        if a.size < 100:
            return None
        # Laplacian 4 hướng, tự cuộn bằng numpy để khỏi kéo theo OpenCV.
        lap = (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
               - 4 * a[1:-1, 1:-1])
        return float(np.var(lap))
    except Exception as e:
        log.warning("Không đo được độ nét: %s", e)
        return None


def _thu_nho_gui_api(data: bytes) -> tuple[bytes, str]:
    """Co ảnh về RONG_GUI_API và nén JPEG — giảm token và thời gian gọi API."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > RONG_GUI_API:
        img = img.resize((RONG_GUI_API,
                          max(1, round(img.height * RONG_GUI_API / img.width))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), "image/jpeg"


# ------------------------------------------------------------ soi nội dung --
_client = None


def _lay_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.AsyncAnthropic()
    return _client


def co_the_soi_noi_dung() -> bool:
    """Có đủ điều kiện gọi Claude vision không."""
    if not BAT_SOI_NOI_DUNG:
        return False
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return False
    from importlib.util import find_spec

    return all(find_spec(m) for m in ("anthropic", "PIL", "numpy"))


async def soi_noi_dung(data: bytes) -> dict | None:
    """Gọi Claude vision phân loại ảnh. None nếu không soi được (lỗi mạng, thiếu key…)."""
    if not co_the_soi_noi_dung():
        return None
    try:
        import base64

        nho, media_type = await asyncio.to_thread(_thu_nho_gui_api, data)
        b64 = base64.standard_b64encode(nho).decode("utf-8")
        resp = await _lay_client().messages.create(
            model=MODEL_SOI_ANH,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text",
                     "text": "Phân loại và chấm ảnh này theo 3 yêu cầu ở trên."},
                ],
            }],
            output_config={
                "format": {"type": "json_schema", "schema": SCHEMA},
                "effort": "low",
            },
        )
        if resp.stop_reason == "refusal":
            log.warning("Model từ chối soi ảnh: %s", resp.stop_details)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        kq = json.loads(text)
        kq["_tokens"] = resp.usage.input_tokens + resp.usage.output_tokens
        return kq
    except Exception:
        log.exception("Lỗi gọi API soi ảnh")
        return None


# ---------------------------------------------------------------- tổng hợp --
async def kiem_tra(data: bytes) -> dict:
    """Soi một ảnh. Trả về dict luôn có các khoá:

    dat      — True/False/None (None = không soi được, coi như cho qua)
    mo       — ảnh có mờ không
    loai     — ngoai_quan | layout | wc | khong_ro | None
    ly_do    — câu giải thích ngắn để nhắn lại cho BC
    do_net   — điểm độ nét đo được
    """
    kq = {"dat": None, "mo": False, "loai": None, "ly_do": "", "do_net": None}

    net = await asyncio.to_thread(do_net, data)
    kq["do_net"] = net
    if net is not None and net < NGUONG_NET:
        kq.update(dat=False, mo=True,
                  ly_do=f"ảnh bị mờ/rung (độ nét {net:.0f}, cần ≥ {NGUONG_NET:.0f})")
        return kq  # đã mờ thì khỏi tốn tiền gọi API

    ai = await soi_noi_dung(data)
    if ai is None:
        return kq

    kq.update(dat=bool(ai.get("dat")), mo=bool(ai.get("mo")),
              loai=ai.get("loai"), ly_do=(ai.get("ly_do") or "").strip())
    if kq["mo"] and not kq["ly_do"]:
        kq["ly_do"] = "ảnh bị mờ, nhìn không rõ chi tiết"
    return kq


def loi_nhan(kq: dict) -> str:
    """Câu nhắn lại BC khi ảnh chưa đạt. Chuỗi rỗng = ảnh ổn, không cần nhắn."""
    if kq.get("dat") is not False:
        return ""
    loai = kq.get("loai") or "khong_ro"
    if kq.get("mo"):
        return (f"📷 <b>Ảnh chưa đạt — {kq['ly_do']}</b>\n"
                f"Đề nghị BC chụp lại <b>nét hơn</b>: giữ máy chắc, bật đèn nếu thiếu sáng, "
                f"chạm vào màn hình để lấy nét rồi mới bấm chụp.")
    return (f"📷 <b>Ảnh chưa đúng yêu cầu — {kq['ly_do']}</b>\n"
            f"Ảnh cần chụp: <b>{TEN_LOAI.get(loai, loai)}</b>\n"
            f"{HUONG_DAN.get(loai, '')}\n"
            f"Đề nghị BC chụp lại và gửi vào group ạ.")
