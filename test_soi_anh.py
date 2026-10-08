"""Kiểm tra khâu soi ảnh: python test_soi_anh.py [đường_dẫn_ảnh ...]

Không có tham số: chạy ảnh tự sinh để kiểm tra thước đo độ nét.
Có tham số: chấm các ảnh thật (nếu đã đặt ANTHROPIC_API_KEY thì soi luôn nội dung).
"""

import asyncio
import io
import sys

sys.stdout.reconfigure(encoding="utf-8")

import soi_anh  # noqa: E402


def _anh_thu(mo: float) -> bytes:
    """Ảnh bàn cờ nhiều biên sắc, làm mờ dần theo bán kính `mo`."""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(img)
    for y in range(0, 600, 40):
        for x in range(0, 900, 40):
            if (x // 40 + y // 40) % 2 == 0:
                d.rectangle([x, y, x + 39, y + 39], fill="black")
    d.text((60, 260), "GiaoHangNhanh", fill="red")
    if mo:
        img = img.filter(ImageFilter.GaussianBlur(mo))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def main() -> int:
    loi = []

    def check(dk, mo_ta):
        print(("  ok  " if dk else "  LỖI ") + mo_ta)
        if not dk:
            loi.append(mo_ta)

    duong_dan = sys.argv[1:]
    if duong_dan:
        print(f"Soi {len(duong_dan)} ảnh thật "
              f"(soi nội dung: {'BẬT' if soi_anh.co_the_soi_noi_dung() else 'TẮT — thiếu API key'})\n")
        for p in duong_dan:
            with open(p, "rb") as f:
                kq = await soi_anh.kiem_tra(f.read())
            net = f"{kq['do_net']:.0f}" if kq["do_net"] is not None else "?"
            print(f"• {p}\n  độ nét {net} · loại {kq['loai']} · đạt {kq['dat']}"
                  f"{' · MỜ' if kq['mo'] else ''}")
            if kq["ly_do"]:
                print(f"  lý do: {kq['ly_do']}")
            nhan = soi_anh.loi_nhan(kq)
            if nhan:
                print("  → nhắn lại BC:")
                print("    " + nhan.replace("\n", "\n    "))
            print()
        return 0

    print("[1] Thước đo độ nét")
    net = soi_anh.do_net(_anh_thu(0))
    mo_nhe = soi_anh.do_net(_anh_thu(2))
    mo_nang = soi_anh.do_net(_anh_thu(6))
    print(f"  nét={net:.0f}  mờ nhẹ={mo_nhe:.0f}  mờ nặng={mo_nang:.0f}  "
          f"(ngưỡng {soi_anh.NGUONG_NET:.0f})")
    check(net > mo_nhe > mo_nang, "càng mờ điểm càng thấp")
    check(net >= soi_anh.NGUONG_NET, "ảnh nét vượt ngưỡng")
    check(mo_nang < soi_anh.NGUONG_NET, "ảnh mờ nặng dưới ngưỡng")
    check(soi_anh.do_net(b"khong phai anh") is None, "dữ liệu hỏng trả None")

    print("\n[2] Luồng kiểm tra")
    kq = await soi_anh.kiem_tra(_anh_thu(6))
    check(kq["dat"] is False and kq["mo"], "ảnh mờ bị chặn ngay, không gọi API")
    check("mờ" in soi_anh.loi_nhan(kq), "có câu nhắn yêu cầu chụp lại nét hơn")

    kq = await soi_anh.kiem_tra(_anh_thu(0))
    if soi_anh.co_the_soi_noi_dung():
        check(kq["loai"] is not None, "ảnh nét được gửi đi soi nội dung")
    else:
        check(kq["dat"] is None and soi_anh.loi_nhan(kq) == "",
              "thiếu API key thì bỏ qua soi nội dung, không chặn ảnh")

    print("\n[3] Câu nhắn theo từng loại lỗi")
    nhan = soi_anh.loi_nhan({"dat": False, "mo": False, "loai": "ngoai_quan",
                             "ly_do": "ảnh chỉ thấy biển hiệu, không thấy bên trong bưu cục"})
    check("biển GHN" in nhan and "bên trong" in nhan, "hướng dẫn chụp lại ảnh ngoại quan")
    check(soi_anh.loi_nhan({"dat": True}) == "", "ảnh đạt thì không nhắn gì")

    print("\n" + "=" * 50)
    print(f"❌ {len(loi)} lỗi: " + "; ".join(loi) if loi else "✅ Toàn bộ kiểm tra đã qua.")
    return 1 if loi else 0


raise SystemExit(asyncio.run(main()))
