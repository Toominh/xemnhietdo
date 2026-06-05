# 🖥️ System Monitor Dashboard

Dashboard theo dõi nhiệt độ CPU/GPU và mức hoạt động CPU, GPU, RAM, SSD theo thời gian thực.

![preview](https://img.shields.io/badge/platform-Windows-blue) ![python](https://img.shields.io/badge/Python-3.8+-green) ![license](https://img.shields.io/badge/license-MIT-orange)

> **Lưu ý:** Trang web chỉ hiển thị dữ liệu khi máy bạn đang chạy `server.py` ở local.

---

## 📋 Yêu cầu

- Windows 10/11
- Python 3.8 trở lên
- Trình duyệt Chrome hoặc Edge

---

## ⚙️ Cài đặt

### Bước 1 — Clone hoặc tải repo về

```bash
git clone https://github.com/username/system-monitor.git
cd system-monitor
```

### Bước 2 — Cài thư viện Python

```bash
pip install psutil
```

### Bước 3 — Tải LibreHardwareMonitor

1. Vào link: [LibreHardwareMonitor Releases](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases)
2. Tải file `.zip` mới nhất → giải nén
3. Chạy `LibreHardwareMonitor.exe`
4. Vào menu **Options → Remote Web Server → Run**

> Kiểm tra: mở trình duyệt vào `http://localhost:8085/data.json` — nếu thấy dữ liệu JSON là thành công ✅

### Bước 4 — Chạy server

```bash
python server.py
```

Terminal hiện:
```
✅  LibreHardwareMonitor HTTP: kết nối thành công!
✅  Server chạy tại  http://localhost:5000/api/stats
```

### Bước 5 — Mở dashboard

Mở file `index.html` **hoặc** truy cập link GitHub Pages bằng Chrome/Edge.

---

## 🗂️ Cấu trúc file

```
system-monitor/
├── index.html       ← Dashboard (mở trực tiếp hoặc qua GitHub Pages)
├── server.py        ← Backend Python đọc dữ liệu phần cứng
└── README.md
```

---

## 📊 Tính năng

| Thông số | Nguồn dữ liệu |
|---|---|
| Nhiệt độ CPU | LibreHardwareMonitor |
| Nhiệt độ GPU | LibreHardwareMonitor / nvidia-smi |
| CPU Usage % | psutil |
| GPU Usage % | nvidia-smi (NVIDIA) / LHM (AMD) |
| RAM Usage | psutil |
| SSD I/O Activity | psutil |

---

## ❓ Hỏi đáp

**Dashboard hiện `--` cho nhiệt độ?**
→ Kiểm tra LibreHardwareMonitor đang chạy và đã bật Remote Web Server (Options → Remote Web Server → Run)

**Dashboard hiện "Đang kết nối server..."?**
→ `server.py` chưa chạy. Mở terminal và chạy `python server.py`

**Dùng Safari không thấy dữ liệu?**
→ Safari chặn kết nối HTTP từ trang HTTPS. Dùng Chrome hoặc Edge thay thế.

**Máy dùng GPU AMD không thấy nhiệt độ GPU?**
→ Đảm bảo LibreHardwareMonitor đang chạy và nhận diện được GPU trong danh sách sensor.

---

## 🔧 Tương thích

| Tính năng | Windows | Linux | macOS |
|---|---|---|---|
| CPU Usage | ✅ | ✅ | ✅ |
| RAM Usage | ✅ | ✅ | ✅ |
| CPU Temp | ✅ (qua LHM) | ✅ (psutil) | ⚠️ hạn chế |
| GPU Temp NVIDIA | ✅ | ✅ | ❌ |
| GPU Temp AMD | ✅ (qua LHM) | ⚠️ | ❌ |
