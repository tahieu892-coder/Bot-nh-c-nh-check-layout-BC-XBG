# Bot nhắc chụp hình layout BC

Bot Telegram ghi nhận ảnh layout/nhà vệ sinh các bưu cục gửi trong group, nhắc lúc **20:30**
và chốt danh sách BC chưa đạt lúc **21:30**, kèm tổng hợp tiền phạt theo AM.

## Cách hoạt động

| Thời điểm | Bot làm gì |
|---|---|
| Trước 18:00 | Ảnh gửi sớm **không được tính** — bot nhắc gửi lại trong khung giờ (mỗi BC nhắc 1 lần/ngày) |
| 18:00 – 21:30 | Khung giờ nhận. BC gửi ảnh kèm caption `Mã BC - Tên BC - Ngày/Tháng/Năm` → bot thả 👍 và đếm ảnh |
| 20:30 | Nhắc: bắn vào **topic riêng của từng AM**, chỉ liệt kê BC của AM đó |
| 21:30 | Chốt: mỗi topic nhận danh sách không đạt + tiền phạt của chính AM đó |
| 21:30 | Topic chung nhận bản tổng hợp toàn vùng (chỉ số liệu, không tag ai) |

### Báo cáo theo topic

Group là forum, mỗi AM một topic. Bot gửi báo cáo vào đúng topic nên mỗi AM chỉ bị tag
**một lần** trong tin của mình, không ai phải đọc dữ liệu của 21 AM còn lại.

Bot tự nhận diện topic: AM nào nhắn tin đầu tiên trong topic của mình, bot khớp nick
Telegram với cột Tele trong sheet rồi tự gắn, có báo lại trong topic đó. Gắn sai thì gõ
`/xoatopic`. Muốn gắn tay thì vào topic gõ `/dangkytopic <tên AM>`.

Xem tiến độ bằng `/dstopic` — liệt kê AM nào đã có topic, AM nào chưa. AM chưa có topic
vẫn được báo, gom vào một tin ở topic chung để không sót ai.

Các lệnh `/thieu`, `/da`, `/chot` gõ **trong topic** thì chỉ trả dữ liệu của AM đó;
gõ ở **General** thì ra toàn vùng.

Bot **không** kiểm tra được ảnh có timemark hay đúng nội dung layout/WC — phần đó vẫn cần
AM mắt thường. Bot lo phần đếm, nhắc và chốt số liệu.

## Cài đặt (10 phút)

### 1. Tạo bot & lấy token

1. Mở Telegram, tìm **@BotFather** (tài khoản có tick xanh) → bấm **Start**.
2. Gõ `/newbot`.
3. BotFather hỏi *"Alright, a new bot. How are we going to call it?"* → nhập **tên hiển thị**,
   tiếng Việt có dấu được: `Bot Nhắc Layout BC`
4. Hỏi tiếp *"Now let's choose a username"* → nhập **username**, bắt buộc kết thúc bằng `bot`
   và không trùng ai: `nhac_layout_bc_bgi_bot`
5. BotFather trả về đoạn token dạng:
   ```
   8123456789:AAH7xK9pQr-vN2mLzT4wYsXcB1dEfGhIjKl
   ```
   Đây chính là `BOT_TOKEN`. **Copy ngay và giữ kín** — ai có token này là điều khiển được bot.
   Lỡ lộ thì gõ `/revoke` để BotFather cấp token mới.

6. **Tắt Privacy Mode** — bắt buộc, không có bước này bot sẽ không thấy ảnh BC gửi trong group:
   - Gõ `/setprivacy` → chọn bot vừa tạo → chọn **Disable**
   - BotFather xác nhận *"Success! The new status is: DISABLED."*

Tuỳ chọn cho đẹp: `/setuserpic` để đặt avatar, `/setdescription` để ghi mô tả.

### 2. Thêm bot vào group
1. Thêm bot vào group vận hành, cấp quyền **Admin** (để thả reaction và đọc mọi tin).
2. Trong group gõ `/id` → bot trả về `chat_id` (số âm) → chép lại.

### 3. Chạy thử trên máy
```bash
cd C:\Users\Admin\bc-layout-bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
Mở `.env`, điền `BOT_TOKEN` và `REPORT_CHAT_ID`, rồi:
```bash
python bot.py
```

### 4. Nạp danh sách BC – AM

Cơ cấu AM thay đổi theo đợt, nên bot lấy danh sách từ **file của anh**, không hardcode.
Chọn 1 trong 3 cách:

**Cách A — Google Sheets (khuyến nghị, tự cập nhật)**

Sheet cần 4 cột (tên cột không phân biệt hoa thường, có dấu hay không):

| Mã BC | Bưu Cục | Họ Tên | Tele |
|---|---|---|---|
| 23009000 | (BGI) Đa Mai | Triệu Ngọc Duy | @DUYBG |

- Cột **Tele** là nick Telegram của AM — bot dùng để `@` đúng người khi nhắc.
- Bot tự dò dòng tiêu đề trong 10 dòng đầu, nên sheet có dòng gộp/tiêu đề phụ phía trên
  vẫn đọc được.
- Cột trùng tên xuất hiện sau (vd cột `AM` / `Tele AM` dùng làm danh sách xổ xuống)
  sẽ bị bỏ qua — mỗi vai trò chỉ lấy cột khớp **đầu tiên từ trái sang**.

Share sheet → **Anyone with the link** (Viewer là đủ) → copy link → đặt vào biến `SHEET_URL`.
Dán link bình thường cũng được, bot tự đổi sang link xuất CSV của đúng tab đang mở (`gid`).

Từ đó mỗi ngày **07:00** bot tự kéo lại. Sửa sheet xong muốn áp dụng ngay thì gõ `/sync`.
Bot chỉ báo vào group khi thực sự có thay đổi, kèm diff rõ ràng:

```
🔄 ĐÃ ĐỒNG BỘ DANH SÁCH BC
Tổng trong file: 45 BC · Giữ nguyên: 41
➕ Thêm mới (1)   • 23009044 (BGI) Yên Dũng — AM: Trần Thị B
🔁 Đổi AM (3)     • 23009002: Nguyễn Văn A → Trần Thị B
➖ Ngừng theo dõi (1)
```

**Cách B — gửi file thẳng vào chat**
Kéo file `.csv` / `.xlsx` vào group, caption ghi `/capnhat`. Bot đọc và cập nhật ngay.
Hợp khi anh giữ file trên máy, không muốn đưa lên Sheets.

**Cách C — gõ tay** (ít BC hoặc sửa lẻ)
```
/importbc
23009000 | (BGI) Đa Mai | Nguyễn Văn A
23009001 | (BGI) Lạng Giang | Nguyễn Văn A
```

Sau khi nạp xong, kiểm tra bằng `/dsbc`.

AM nào **không có nick trong cột Tele** thì bot chỉ ghi tên, không tag được. Người đó gõ
`/setam <tên AM>` (đúng tên trong sheet) một lần trong group là bot tag được từ đó về sau.
Bot cũng liệt kê sẵn các AM thiếu nick sau mỗi lần đồng bộ.

> File thiếu cột / sai định dạng: bot đọc được cả file có lẫn không có dòng tiêu đề, tự nhận
> cột theo tên ("Mã BC", "Tên BC", "AM" — không phân biệt hoa thường, có dấu hay không).
> Nếu file mới ít hơn 50% số BC đang theo dõi, bot **không** ngừng theo dõi BC nào cả và cảnh
> báo — đề phòng file lỗi làm mất danh sách.

### 5. Deploy lên Railway (chạy 24/7)
1. Đẩy thư mục này lên một repo GitHub **private**.
2. Vào [railway.app](https://railway.app) → *New Project* → *Deploy from GitHub repo*.
3. Tab **Variables**: thêm `BOT_TOKEN`, `REPORT_CHAT_ID`, `SHEET_URL`, `TZ=Asia/Ho_Chi_Minh`, `DB_PATH=/data/bot.db`.
4. Tab **Settings** → *Volumes* → *Add Volume*, mount path `/data`. **Không có bước này, dữ liệu sẽ mất mỗi lần deploy lại.**
5. Deploy. Xem tab *Deploy Logs*, thấy dòng `Bot đang chạy…` là xong.

> Dùng Render thay Railway cũng được, nhưng gói free của Render không có background worker —
> phải tạo *Web Service* và để nguyên biến `PORT` (code đã có sẵn health server để giữ bot sống).

## Lệnh

**Nhân viên:** `/gan <mã BC>` (gán 1 lần, sau đó gửi ảnh không cần gõ mã), `/huygan`,
`/thieu`, `/da`, `/id`

**Quản lý:** `/sync` (kéo lại danh sách từ Sheets), `/thembc`, `/importbc`, `/xoabc`,
`/dsbc`, `/setam`, `/reset <mã>`, `/nhac`, `/chot`, `/tuan`, `/lich`

## Tuỳ chỉnh

Sửa biến môi trường, không cần sửa code:

- `GIO_NHAC=18:00,20:30` — nhắc nhiều lần trong ngày
- `GIO_CHOT=21:30`, `SO_ANH_YEU_CAU=2`, `MUC_PHAT=100000`
- `ADMIN_IDS=123456,789012` — giới hạn ai được dùng lệnh quản trị
- `ALLOWED_CHAT_IDS=-100111,-100222` — nhận ảnh từ nhiều group, báo cáo về `REPORT_CHAT_ID`
- `SHEET_URL`, `GIO_SYNC=07:00` — nguồn và giờ đồng bộ danh sách BC–AM
- `BO_QUA_BC`, `BO_QUA_TU_KHOA` — loại các dòng không phải BC cần chụp layout khỏi sheet.
  Mặc định lọc theo từ khoá `kho chuyen tiep,kct` nên KCT mở mới về sau tự động bị loại,
  không phải sửa cấu hình lại.

## Lưu ý vận hành

- Bot đếm theo **số ảnh**, không phân biệt được đâu là layout đâu là nhà vệ sinh. BC gửi 2 ảnh
  layout vẫn tính đủ — AM cần soát ngẫu nhiên.
- Ảnh gửi dạng **album** vẫn đếm đủ (chỉ ảnh đầu cần caption).
- BC gửi nhầm mã: quản lý gõ `/reset <mã>` để xoá ghi nhận trong ngày rồi gửi lại.
- Ngày trong caption khác ngày hệ thống → bot cảnh báo ngay tại tin nhắn đó.
