#!/usr/bin/env python3
"""
System Monitor Backend
Tối ưu cho: Lenovo ThinkPad P16s Gen 3
CPU : Intel Core Ultra 7 (Meteor Lake)
GPU : NVIDIA RTX 500 Ada Generation (discrete) + Intel Arc (iGPU)
OS  : Windows 10/11
"""

import json, time, platform, subprocess, threading, urllib.request, os, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    print("LỖI: pip install psutil"); exit(1)

LHM_URL = "http://localhost:8085/data.json"

print("=" * 55)
print("  System Monitor — ThinkPad P16s Gen 3")
print("=" * 55)

# ── Tìm nvidia-smi (RTX 500 Ada) ──────────────────────────────────────────────
def find_nvidia_smi():
    # Thử trong PATH trước
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return "nvidia-smi"
    except Exception:
        pass

    # Tìm trong các đường dẫn phổ biến trên Windows
    candidates = [
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Windows\SysWOW64\nvidia-smi.exe",
    ]
    for pattern in [
        r"C:\Program Files\NVIDIA*\*\nvidia-smi.exe",
        r"C:\Program Files (x86)\NVIDIA*\*\nvidia-smi.exe",
    ]:
        candidates.extend(glob.glob(pattern))

    for path in candidates:
        if os.path.isfile(path):
            try:
                r = subprocess.run(
                    [path, "--query-gpu=name", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    return path
            except Exception:
                pass
    return None

NVIDIA_SMI = find_nvidia_smi()
GPU_NVIDIA  = NVIDIA_SMI is not None

if GPU_NVIDIA:
    r = subprocess.run(
        [NVIDIA_SMI, "--query-gpu=name", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=3)
    print(f"✅  NVIDIA GPU  : {r.stdout.strip()}")
    print(f"   nvidia-smi  : {NVIDIA_SMI}")
else:
    print("⚠️  nvidia-smi không tìm thấy — GPU temp qua LHM")

# ── LHM fetch & parse ─────────────────────────────────────────────────────────
_lhm_cache = []

def fetch_lhm():
    """Lấy hardware list từ LHM HTTP, fallback cache khi mất kết nối."""
    global _lhm_cache
    try:
        req = urllib.request.Request(LHM_URL)
        req.add_header("User-Agent", "SystemMonitor/1.0")
        with urllib.request.urlopen(req, timeout=4) as resp:
            root = json.loads(resp.read().decode())
    except Exception:
        return _lhm_cache   # trả cache cũ khi LHM tạm thời không phản hồi

    def parse_val(s):
        try:
            return float(s.split()[0].replace(",", "."))
        except Exception:
            return None

    def classify(name):
        n = name.lower()
        # ── CPU: Intel Core Ultra (Meteor Lake) ──
        if any(x in n for x in [
            "core ultra", "core i", "intel core", "xeon", "ryzen",
            "athlon", "cpu", "i3-", "i5-", "i7-", "i9-",
            "ultra 5", "ultra 7", "ultra 9"
        ]):
            return "cpu"
        # ── GPU rời: RTX 500 Ada + Intel Arc iGPU ──
        if any(x in n for x in [
            "rtx", "gtx", "quadro", "geforce", "radeon", "rx ",
            "vega", "intel arc", "arc graphics", "intel graphics",
            "iris xe", "uhd graphics", "gpu"
        ]):
            return "gpu"
        # ── RAM ──
        if any(x in n for x in ["ddr", "dimm", "generic memory", "ram"]):
            return "ram"
        # ── Storage: NVMe / SSD (laptop không có HDD) ──
        if any(x in n for x in [
            "nvme", "ssd", "samsung", "sk hynix", "kioxia", "micron",
            "western digital", "wd ", "wdc", "kingston", "crucial",
            "seagate", "toshiba", "intel ssd", "sabrent", "ct"
        ]):
            return "storage"
        return "other"

    def collect_temps(node):
        result = []
        val = node.get("Value", "")
        if "°c" in val.lower():
            v = parse_val(val)
            if v and 10 < v < 120:
                result.append({"name": node.get("Text", ""), "temp": round(v, 1)})
        for child in node.get("Children", []):
            result.extend(collect_temps(child))
        return result

    def collect_loads(node):
        result = []
        val = node.get("Value", "")
        if "%" in val:
            v = parse_val(val)
            if v is not None and 0 <= v <= 100:
                result.append({"name": node.get("Text", ""), "load": round(v, 1)})
        for child in node.get("Children", []):
            result.extend(collect_loads(child))
        return result

    hardware = []
    computers = root.get("Children", [])
    hw_nodes  = computers[0].get("Children", []) if computers else []

    for hw in hw_nodes:
        hw_name = hw.get("Text", "")
        hw_type = classify(hw_name)
        hardware.append({
            "name":  hw_name,
            "type":  hw_type,
            "temps": collect_temps(hw),
            "loads": collect_loads(hw),
        })

    _lhm_cache = hardware
    return hardware

# ── Trích xuất từng loại sensor ───────────────────────────────────────────────
def get_cpu_temp(hw_list):
    """
    Nhiệt độ CPU — khớp với Speccy/HWiNFO.
    Ưu tiên: CPU Package → Core Average → trung bình các core → core đầu tiên.
    KHÔNG dùng max() vì sẽ lấy hotspot cao hơn thực tế.
    """
    for hw in hw_list:
        if hw["type"] != "cpu":
            continue
        temps = hw["temps"]
        if not temps:
            continue

        names = {t["name"].lower(): t["temp"] for t in temps}

        # 1. CPU Package — giống Speccy nhất
        for k, v in names.items():
            if "package" in k:
                return v

        # 2. Core Average (LHM có thể expose sẵn)
        for k, v in names.items():
            if "average" in k or "avg" in k:
                return v

        # 3. Tdie / Tctl (AMD) hoặc CPU (generic)
        for keyword in ["tdie", "tctl", "cpu temperature"]:
            for k, v in names.items():
                if keyword in k:
                    return v

        # 4. Tính trung bình các individual core (P-core + E-core)
        core_vals = [
            v for k, v in names.items()
            if ("core" in k or "p-core" in k or "e-core" in k)
            and "max" not in k and "average" not in k
        ]
        if core_vals:
            return round(sum(core_vals) / len(core_vals), 1)

        # 5. Last resort: sensor đầu tiên (không dùng max)
        return temps[0]["temp"]
    return None

def get_gpu_nvidia():
    """Đọc RTX 500 Ada qua nvidia-smi."""
    if not GPU_NVIDIA:
        return None, None
    try:
        r = subprocess.run(
            [NVIDIA_SMI,
             "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            parts = r.stdout.strip().split(",")
            if len(parts) >= 2:
                return float(parts[0].strip()), float(parts[1].strip())
    except Exception:
        pass
    return None, None

def get_gpu_stats(hw_list):
    """GPU: nvidia-smi cho RTX 500, fallback LHM cho Intel Arc."""
    # RTX 500 Ada qua nvidia-smi (chính xác nhất)
    gt, gu = get_gpu_nvidia()
    if gt is not None:
        return gt, gu

    # Intel Arc iGPU qua LHM
    for hw in hw_list:
        if hw["type"] != "gpu":
            continue
        gt = hw["temps"][0]["temp"] if hw["temps"] else None
        gu = None
        for load in hw["loads"]:
            if "core" in load["name"].lower():
                gu = load["load"]
                break
        if gt or gu:
            return gt, gu

    return None, None

def get_storage_temps(hw_list):
    """Nhiệt độ ổ NVMe/SSD."""
    result = []
    for hw in hw_list:
        if hw["type"] != "storage" or not hw["temps"]:
            continue
        result.append({"name": hw["name"], "temp": hw["temps"][0]["temp"]})
    return result

def get_ram_temp(hw_list):
    """Nhiệt độ RAM — thường chỉ có trên DDR5 hoặc ECC."""
    for hw in hw_list:
        if hw["type"] == "ram" and hw["temps"]:
            return hw["temps"][0]["temp"]
    return None

# ── Disk I/O ──────────────────────────────────────────────────────────────────
_pd, _pt = None, None

def get_disk_activity():
    global _pd, _pt
    try:
        c   = psutil.disk_io_counters()
        now = time.time()
        if _pd is None:
            _pd = c; _pt = now; return 0.0
        dt = now - _pt
        if dt < 0.01:
            return 0.0
        mb = (c.read_bytes - _pd.read_bytes +
              c.write_bytes - _pd.write_bytes) / dt / 1_048_576
        _pd = c; _pt = now
        return round(min(100.0, mb / 3500 * 100), 1)  # NVMe ceiling ~3500 MB/s
    except Exception:
        return 0.0

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache: dict = {}
_lock         = threading.Lock()
_lhm_ok       = False

def collect():
    global _lhm_ok
    try:
        hw_list             = fetch_lhm()
        _lhm_ok             = bool(hw_list)
        cpu_temp            = get_cpu_temp(hw_list)
        gpu_temp, gpu_use   = get_gpu_stats(hw_list)
        storage_temps       = get_storage_temps(hw_list)
        ram_temp            = get_ram_temp(hw_list)
        cpu_pct             = psutil.cpu_percent(interval=None)
        cpu_freq            = psutil.cpu_freq()
        ram                 = psutil.virtual_memory()

        with _lock:
            _cache.update({
                "ts": round(time.time() * 1000),
                "cpu": {
                    "usage":   round(cpu_pct, 1),
                    "temp":    cpu_temp,
                    "freq":    round(cpu_freq.current / 1000, 2) if cpu_freq else None,
                    "cores":   psutil.cpu_count(logical=False),
                    "threads": psutil.cpu_count(logical=True),
                },
                "gpu": {
                    "usage": round(gpu_use, 1)  if gpu_use  is not None else None,
                    "temp":  round(gpu_temp, 1) if gpu_temp is not None else None,
                },
                "ram": {
                    "usage":    round(ram.percent, 1),
                    "used_gb":  round(ram.used  / 1024**3, 2),
                    "total_gb": round(ram.total / 1024**3, 2),
                    "temp":     ram_temp,
                },
                "ssd": {
                    "activity": get_disk_activity(),
                    "drives":   storage_temps,
                },
                "platform": platform.system(),
                "lhm_ok":   _lhm_ok,
            })
    except Exception as e:
        print(f"[collect error] {e}")

def poll():
    psutil.cpu_percent(interval=None)   # prime
    ok_logged  = False
    fail_count = 0
    while True:
        try:
            collect()
            if _lhm_ok and not ok_logged:
                print("✅  LHM HTTP    : kết nối OK")
                ok_logged  = True
                fail_count = 0
            elif not _lhm_ok:
                fail_count += 1
                if fail_count == 1:
                    print("⚠️  LHM mất kết nối — dùng cache...")
                ok_logged = False
        except Exception as e:
            print(f"[poll error] {e}")
        time.sleep(1.5)

# ── HTTP server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/stats":
            try:
                with _lock:
                    body = json.dumps(_cache).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[http error] {e}")
                self.send_response(500); self.end_headers()

        elif path == "/api/debug":
            try:
                hw  = fetch_lhm()
                out = [{"name": h["name"], "type": h["type"],
                        "temps": h["temps"], "loads": h["loads"]} for h in hw]
                body = json.dumps(out, indent=2, ensure_ascii=False).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[debug error] {e}")
                self.send_response(500); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    HOST, PORT = "localhost", 5000
    collect()
    threading.Thread(target=poll, daemon=True).start()
    print(f"\n✅  API : http://{HOST}:{PORT}/api/stats")
    print("   Debug: http://localhost:5000/api/debug")
    print("   Mở index.html trong trình duyệt.\n")
    try:
        HTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer dừng.")
