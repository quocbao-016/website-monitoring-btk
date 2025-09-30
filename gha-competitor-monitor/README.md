# Competitor Website Monitor (GitHub Actions)

Theo dõi URL mới, URL biến mất, và thay đổi nội dung (text) cho nhiều website đối thủ.
Thông báo qua Slack (Incoming Webhook). Lưu trạng thái vào `state.json`.

## Cách dùng nhanh
1. Tạo repo GitHub trống và upload các file trong gói này (giữ nguyên cấu trúc thư mục).
2. Vào **Settings → Secrets and variables → Actions → New repository secret**:
   - Name: `SLACK_WEBHOOK_URL`
   - Value: URL webhook bạn tạo trong Slack.
3. Kiểm tra `sites.yml` đã đúng domain/phạm vi theo dõi.
4. Vào tab **Actions → Monitor Competitor Sites → Run workflow** để chạy thử.
5. Xem kết quả ở Slack và logs trong tab **Actions**.

## Lịch chạy
Mặc định chạy hàng ngày lúc **01:20 UTC** (~08:20 Việt Nam). Bạn có thể chỉnh cron trong `.github/workflows/monitor.yml`.

## Tùy chỉnh
- Sửa `sites.yml` để thêm/bớt site, giới hạn URL, ngưỡng thay đổi, timeout, v.v.
- Sửa `monitor.py` nếu muốn hash theo selector cụ thể cho từng site.

