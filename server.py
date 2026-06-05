#!/usr/bin/env python3
"""System Monitor Backend — Stable version với retry & caching"""

import json, time, platform, subprocess, threading, urllib.request, os, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    print("LỖI: pip install psutil"); exit(1)

LHM_URL = "http://localhost:8085/data.json"

# ── NVIDIA nvidia-smi ─────────────────────────────────────────────────────────
def find_nvidia_smi():
    """Tìm nvidia-smi.exe trên Windows (không cần PATH)."""
    try:
        r = subprocess.run(["nvidia-smi","--query-gpu=name","--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return "nvidia-smi"
    except Exception:
        pass
    
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
                r = subprocess.run([path,"--query-gpu=name","--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    return path
            except Exception:
                pass
    return None

NVIDIA_SMI = find_nvidia_smi()
GPU_NVIDIA  = NVIDIA_SMI is not None
if GPU_NVIDIA:
    try:
        r = subprocess.run([NVIDIA_SMI,"--query-gpu=name","--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3)
        print(f"✅  NVIDIA GPU: {r.stdout.strip()}")
        print(f"   nvidia-smi: {NVIDIA_SMI}\n")
    except Exception:
        pass
else:
    print("⚠️  Không tìm thấy nvidia-smi.exe\n")

# ── LHM fetch & walk ──────────────────────────────────────────────────────────
_lhm_cache = []

def fetch_lhm():
    """Lấy hardware nodes từ LHM HTTP, dùng cache khi fail."""
    global _lhm_cache
    
    try:
        req = urllib.request.Request(LHM_URL)
        req.add_header("User-Agent", "SystemMonitor/1.0")
        with urllib.request.urlopen(req, timeout=4) as resp:
            root = json.loads(resp.read().decode())
    except Exception as e:
        # Nếu fail, dùng cache cũ để tránh bị mất dữ liệu
        return _lhm_cache

    def parse_val(s):
        try:
            return float(s.split()[0].replace(",","."))
        except Exception:
            return None

    def classify(name):
        n = name.lower()
        if any(x in n for x in ["core i","ryzen","intel core","xeon","cpu","athlon"]):
            return "cpu"
        if any(x in n for x in ["geforce","radeon","gpu","gtx","rtx","rx ","arc ","vega","quadro"]):
            return "gpu"
        if any(x in n for x in ["ddr","dimm","generic memory","ram memory"]):
            return "ram"
        if any(x in n for x in ["ssd","hdd","nvme","samsung","seagate","wd ","western",
                                  "kingston","crucial","toshiba","hitachi","st1","wdc",
                                  "ct","micron","sk hynix","intel ssd","sabrent"]):
            return "storage"
        return "other"

    def collect_temps(node):
        result = []
        val = node.get("Value","")
        if "°c" in val.lower():
            v = parse_val(val)
            if v and 10 < v < 120:
                result.append({"name": node.get("Text",""), "temp": round(v,1)})
        for child in node.get("Children",[]):
            result.extend(collect_temps(child))
        return result

    def collect_loads(node):
        result = []
        val = node.get("Value","")
        if "%" in val:
            v = parse_val(val)
            if v is not None and 0 <= v <= 100:
                result.append({"name": node.get("Text",""), "load": round(v,1)})
        for child in node.get("Children",[]):
            result.extend(collect_loads(child))
        return result

    hardware = []
    computers = root.get("Children",[])
    hw_nodes  = computers[0].get("Children",[]) if computers else []

    for hw in hw_nodes:
        hw_name = hw.get("Text","")
        hw_type = classify(hw_name)
        temps   = collect_temps(hw)
        loads   = collect_loads(hw)
        hardware.append({"name": hw_name, "type": hw_type,
                         "temps": temps, "loads": loads})
    
    _lhm_cache = hardware
    return hardware

# ── Parsers per type ──────────────────────────────────────────────────────────
def get_cpu_temp(hw_list):
    for hw in hw_list:
        if hw["type"] != "cpu": continue
        for t in hw["temps"]:
            n = t["name"].lower()
            if any(x in n for x in ["package","tdie","tctl","cpu"]):
                return t["temp"]
        if hw["temps"]:
            return max(t["temp"] for t in hw["temps"])
    return None

def get_gpu_stats(hw_list):
    if GPU_NVIDIA:
        try:
            r = subprocess.run(
                [NVIDIA_SMI,"--query-gpu=temperature.gpu,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                parts = r.stdout.strip().split(",")
                return float(parts[0].strip()), float(parts[1].strip())
        except Exception:
            pass
    for hw in hw_list:
        if hw["type"] != "gpu": continue
        gt = hw["temps"][0]["temp"] if hw["temps"] else None
        gu = None
        for l in hw["loads"]:
            if "core" in l["name"].lower():
                gu = l["load"]; break
        return gt, gu
    return None, None

def get_storage_temps(hw_list):
    result = []
    for hw in hw_list:
        if hw["type"] != "storage": continue
        if not hw["temps"]: continue
        temp = hw["temps"][0]["temp"]
        result.append({"name": hw["name"], "temp": temp})
    return result

def get_ram_temp(hw_list):
    for hw in hw_list:
        if hw["type"] != "ram": continue
        if hw["temps"]:
            return hw["temps"][0]["temp"]
    return None

# ── disk I/O ──────────────────────────────────────────────────────────────────
_pd, _pt = None, None
def get_disk_activity():
    global _pd, _pt
    try:
        c = psutil.disk_io_counters(); now = time.time()
        if _pd is None: _pd = c; _pt = now; return 0.0
        dt = now - _pt
        if dt < 0.01: return 0.0
        mb = (c.read_bytes-_pd.read_bytes+c.write_bytes-_pd.write_bytes)/dt/1048576
        _pd = c; _pt = now
        return round(min(100.0, mb/500*100), 1)
    except Exception:
        return 0.0

# ── cache ─────────────────────────────────────────────────────────────────────
_cache, _lock = {}, threading.Lock()
_lhm_ok = False

def collect():
    global _lhm_ok
    try:
        hw_list  = fetch_lhm()
        _lhm_ok  = bool(hw_list)

        cpu_temp            = get_cpu_temp(hw_list)
        gpu_temp, gpu_use   = get_gpu_stats(hw_list)
        storage_temps       = get_storage_temps(hw_list)
        ram_temp            = get_ram_temp(hw_list)
        
        cpu_pct  = psutil.cpu_percent(interval=None)
        cpu_freq = psutil.cpu_freq()
        ram      = psutil.virtual_memory()

        with _lock:
            _cache.update({
                "ts": round(time.time()*1000),
                "cpu": {
                    "usage":   round(cpu_pct,1),
                    "temp":    cpu_temp,
                    "freq":    round(cpu_freq.current/1000,2) if cpu_freq else None,
                    "cores":   psutil.cpu_count(logical=False),
                    "threads": psutil.cpu_count(logical=True),
                },
                "gpu": {
                    "usage": round(gpu_use,1) if gpu_use is not None else None,
                    "temp":  round(gpu_temp,1) if gpu_temp is not None else None,
                },
                "ram": {
                    "usage":    round(ram.percent,1),
                    "used_gb":  round(ram.used/1024**3,2),
                    "total_gb": round(ram.total/1024**3,2),
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
    psutil.cpu_percent(interval=None)
    ok_logged = False
    fail_count = 0
    while True:
        try:
            collect()
            if _lhm_ok and not ok_logged:
                print("✅  LHM HTTP: kết nối OK")
                ok_logged = True
                fail_count = 0
            elif not _lhm_ok:
                fail_count += 1
                if fail_count == 1:
                    print("⚠️  LHM bị mất kết nối, dùng cache...")
                ok_logged = False
        except Exception as e:
            print(f"[poll error] {e}")
        time.sleep(1.5)

# ── HTTP ──────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    
    def do_OPTIONS(self): 
        self.send_response(200); self._c(); self.end_headers()
    
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/stats":
            try:
                with _lock: 
                    body = json.dumps(_cache).encode()
                self.send_response(200); self._c()
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[stats error] {e}")
                self.send_response(500); self.end_headers()
        elif path == "/api/debug":
            try:
                hw_list = fetch_lhm()
                out = []
                for hw in hw_list:
                    out.append({
                        "name": hw["name"],
                        "type": hw["type"],
                        "temps": hw["temps"],
                        "loads": hw["loads"],
                    })
                body = json.dumps(out, indent=2, ensure_ascii=False).encode()
                self.send_response(200); self._c()
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[debug error] {e}")
                self.send_response(500); self.end_headers()
        else:
            self.send_response(404); self.end_headers()
    
    def _c(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")

if __name__ == "__main__":
    print("\n📡  Starting System Monitor...\n")
    collect()
    threading.Thread(target=poll, daemon=True).start()
    print(f"✅  Server: http://localhost:5000/api/stats")
    print("   Mở index.html trong trình duyệt.\n")
    try:
        HTTPServer(("localhost",5000), H).serve_forever()
    except KeyboardInterrupt:
        print("Dừng.")
