#!/usr/bin/env python3
"""
System Monitor Backend — ThinkPad P16s Gen 3
Architecture: 3 threads độc lập (LHM / nvidia-smi / psutil)
Không cái nào block cái nào → kết nối ổn định.
"""

import json, time, platform, subprocess, threading, urllib.request, os, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    print("LỖI: pip install psutil"); exit(1)

LHM_URL = "http://localhost:8085/data.json"

print("=" * 55)
print("  System Monitor — ThinkPad P16s Gen 3")
print("=" * 55)

# ── Tìm nvidia-smi ─────────────────────────────────────────────────────────────
def find_nvidia_smi():
    try:
        r = subprocess.run(["nvidia-smi","--query-gpu=name","--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return "nvidia-smi"
    except Exception:
        pass
    candidates = [
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Windows\SysWOW64\nvidia-smi.exe",
    ]
    for pat in [r"C:\Program Files\NVIDIA*\*\nvidia-smi.exe",
                r"C:\Program Files (x86)\NVIDIA*\*\nvidia-smi.exe"]:
        candidates.extend(glob.glob(pat))
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
if NVIDIA_SMI:
    r = subprocess.run([NVIDIA_SMI,"--query-gpu=name","--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=3)
    print(f"✅  NVIDIA GPU : {r.stdout.strip()}")
    print(f"   nvidia-smi : {NVIDIA_SMI}")
else:
    print("⚠️  nvidia-smi không tìm thấy")

# ══════════════════════════════════════════════════════════════════════════════
# SHARED STATE — mỗi thread ghi vào vùng riêng, HTTP server chỉ đọc
# ══════════════════════════════════════════════════════════════════════════════
_state = {
    "lhm":    {},   # dữ liệu từ LHM thread
    "nvidia": {},   # dữ liệu từ nvidia-smi thread
    "sys":    {},   # dữ liệu từ psutil thread
}
_state_lock = threading.Lock()

# ── Helper ─────────────────────────────────────────────────────────────────────
def parse_val(s):
    try:
        return float(s.split()[0].replace(",", "."))
    except Exception:
        return None

def classify(name):
    n = name.lower()
    if any(x in n for x in ["core ultra","core i","intel core","xeon","ryzen",
                              "athlon","ultra 5","ultra 7","ultra 9"]):
        return "cpu"
    if any(x in n for x in ["rtx","gtx","quadro","geforce","radeon","rx ",
                              "vega","intel arc","arc graphics","iris xe",
                              "uhd graphics","gpu"]):
        return "gpu"
    if any(x in n for x in ["ddr","dimm","generic memory","ram"]):
        return "ram"
    if any(x in n for x in ["nvme","ssd","samsung","sk hynix","kioxia","micron",
                              "western digital","wd ","wdc","kingston","crucial",
                              "seagate","toshiba","intel ssd","sabrent","ct"]):
        return "storage"
    if "batter" in n or "acpi" in n:
        return "battery"
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

# ══════════════════════════════════════════════════════════════════════════════
# THREAD 1: LHM — cập nhật mỗi 2s
# ══════════════════════════════════════════════════════════════════════════════
_lhm_hw_cache = []

def lhm_thread():
    global _lhm_hw_cache
    ok_logged = False
    while True:
        try:
            req = urllib.request.Request(LHM_URL)
            req.add_header("User-Agent","SystemMonitor/1.0")
            with urllib.request.urlopen(req, timeout=3) as resp:
                root = json.loads(resp.read().decode())

            hardware = []
            computers = root.get("Children",[])
            hw_nodes  = computers[0].get("Children",[]) if computers else []
            for hw in hw_nodes:
                hardware.append({
                    "name":  hw.get("Text",""),
                    "type":  classify(hw.get("Text","")),
                    "temps": collect_temps(hw),
                    "loads": collect_loads(hw),
                })
            _lhm_hw_cache = hardware

            # Trích xuất
            cpu_temp  = _parse_cpu_temp(hardware)
            gpu_lhm   = _parse_gpu_lhm(hardware)
            storage   = _parse_storage(hardware)
            ram_temp  = _parse_ram_temp(hardware)
            bat_lhm   = _parse_battery_lhm(hardware)

            with _state_lock:
                _state["lhm"] = {
                    "ok":       True,
                    "cpu_temp": cpu_temp,
                    "gpu":      gpu_lhm,
                    "storage":  storage,
                    "ram_temp": ram_temp,
                    "bat":      bat_lhm,
                }
            if not ok_logged:
                print("✅  LHM HTTP   : kết nối OK")
                ok_logged = True

        except Exception:
            with _state_lock:
                if _state["lhm"].get("ok"):
                    _state["lhm"]["ok"] = False
                    ok_logged = False
                    print("⚠️  LHM mất kết nối — dùng cache")
            # Giữ cache cũ, không xoá data
        time.sleep(2)

def _parse_cpu_temp(hw_list):
    for hw in hw_list:
        if hw["type"] != "cpu": continue
        temps = hw["temps"]
        if not temps: continue
        names = {t["name"].lower(): t["temp"] for t in temps}
        for k,v in names.items():
            if "package" in k: return v
        for k,v in names.items():
            if "average" in k or "avg" in k: return v
        for kw in ["tdie","tctl","cpu temperature"]:
            for k,v in names.items():
                if kw in k: return v
        core_vals = [v for k,v in names.items()
                     if ("core" in k or "p-core" in k or "e-core" in k)
                     and "max" not in k and "average" not in k]
        if core_vals:
            return round(sum(core_vals)/len(core_vals), 1)
        return temps[0]["temp"]
    return None

def _parse_gpu_lhm(hw_list):
    for hw in hw_list:
        if hw["type"] != "gpu": continue
        gt = hw["temps"][0]["temp"] if hw["temps"] else None
        gu = None
        for l in hw["loads"]:
            if "core" in l["name"].lower(): gu = l["load"]; break
        return {"temp": gt, "usage": gu}
    return {"temp": None, "usage": None}

def _parse_storage(hw_list):
    result = []
    for hw in hw_list:
        if hw["type"] != "storage" or not hw["temps"]: continue
        result.append({"name": hw["name"], "temp": hw["temps"][0]["temp"]})
    return result

def _parse_ram_temp(hw_list):
    for hw in hw_list:
        if hw["type"] == "ram" and hw["temps"]:
            return hw["temps"][0]["temp"]
    return None

def _parse_battery_lhm(hw_list):
    for hw in hw_list:
        if hw["type"] != "battery": continue
        temp, watts = None, None
        for t in hw["temps"]:
            if temp is None and 0 < t["temp"] < 80: temp = t["temp"]
        for l in hw["loads"]:
            ln = l["name"].lower()
            if "charge rate" in ln or "watt" in ln or "power" in ln:
                watts = l["load"]
        return {"temp": temp, "watts": watts}
    return {"temp": None, "watts": None}

# ══════════════════════════════════════════════════════════════════════════════
# THREAD 2: nvidia-smi — cập nhật mỗi 3s (subprocess chậm hơn)
# ══════════════════════════════════════════════════════════════════════════════
def nvidia_thread():
    if not NVIDIA_SMI:
        return
    while True:
        try:
            r = subprocess.run(
                [NVIDIA_SMI,
                 "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    with _state_lock:
                        _state["nvidia"] = {
                            "temp":     float(parts[0]),
                            "usage":    float(parts[1]),
                            "vram_used":  round(float(parts[2])/1024, 2),
                            "vram_total": round(float(parts[3])/1024, 2),
                        }
        except Exception:
            pass   # giữ data cũ, không block
        time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# THREAD 3: psutil — cập nhật mỗi 1.5s (nhanh, không block)
# ══════════════════════════════════════════════════════════════════════════════
_pd, _pt = None, None
_pn, _pnt = None, None

def get_disk_stats():
    """Trả về (read_mbps, write_mbps, activity_pct)."""
    global _pd, _pt
    try:
        c = psutil.disk_io_counters(); now = time.time()
        if _pd is None: _pd=c; _pt=now; return 0.0, 0.0, 0.0
        dt = now - _pt
        if dt < 0.01: return 0.0, 0.0, 0.0
        r_mb = (c.read_bytes  - _pd.read_bytes)  / dt / 1_048_576
        w_mb = (c.write_bytes - _pd.write_bytes) / dt / 1_048_576
        _pd=c; _pt=now
        act = round(min(100.0, (r_mb+w_mb)/3500*100), 1)
        return round(r_mb,2), round(w_mb,2), act
    except Exception:
        return 0.0, 0.0, 0.0

def get_network_stats():
    """Trả về (down_mbps, up_mbps) của interface đang dùng (WiFi ưu tiên)."""
    global _pn, _pnt
    try:
        now   = time.time()
        stats = psutil.net_io_counters(pernic=True)
        # Ưu tiên WiFi / WLAN, rồi mới Ethernet, bỏ loopback
        prefer = ["wi-fi","wifi","wlan","wireless","wlp","wl0"]
        chosen = None
        for name, s in stats.items():
            nl = name.lower()
            if any(x in nl for x in prefer):
                chosen = (name, s); break
        if chosen is None:
            for name, s in stats.items():
                nl = name.lower()
                if "lo" in nl or "loopback" in nl or "virtual" in nl: continue
                if s.bytes_recv + s.bytes_sent > 0:
                    chosen = (name, s); break
        if chosen is None:
            return 0.0, 0.0, "N/A"
        name, cur = chosen
        if _pn is None or name not in _pn:
            _pn  = {name: cur}
            _pnt = now
            return 0.0, 0.0, name
        dt = now - _pnt
        if dt < 0.01: return 0.0, 0.0, name
        prev = _pn.get(name, cur)
        down = (cur.bytes_recv - prev.bytes_recv) / dt / 1_048_576
        up   = (cur.bytes_sent - prev.bytes_sent) / dt / 1_048_576
        _pn[name] = cur; _pnt = now
        return round(max(0,down),3), round(max(0,up),3), name
    except Exception:
        return 0.0, 0.0, "N/A" 

def get_battery_health():
    try:
        import wmi as _wmi
        w = _wmi.WMI(namespace="root\\wmi")
        full_list   = w.BatteryFullChargedCapacity()
        design_list = w.BatteryStaticData()
        if full_list and design_list:
            full   = full_list[0].FullChargedCapacity
            design = design_list[0].DesignedCapacity
            if design > 0:
                return round(full/design*100, 1), full, design
    except Exception:
        pass
    return None, None, None

_bat_health_cache = (None, None, None)
_bat_health_ts    = 0

def psutil_thread():
    global _bat_health_cache, _bat_health_ts
    psutil.cpu_percent(interval=None)   # prime
    while True:
        try:
            cpu_pct  = psutil.cpu_percent(interval=None)
            cpu_freq = psutil.cpu_freq()
            ram      = psutil.virtual_memory()
            bat_raw  = psutil.sensors_battery()
            disk_r, disk_w, disk_act = get_disk_stats()
            net_down, net_up, net_iface = get_network_stats()

            # Battery health: đọc mỗi 60s (WMI chậm)
            now = time.time()
            if now - _bat_health_ts > 60:
                _bat_health_cache = get_battery_health()
                _bat_health_ts    = now
            health, full_cap, design_cap = _bat_health_cache

            battery = None
            if bat_raw:
                status = ("charging"    if bat_raw.power_plugged and bat_raw.percent < 100 else
                          "full"        if bat_raw.power_plugged else
                          "discharging")
                tl = None
                if bat_raw.secsleft and bat_raw.secsleft > 0 and bat_raw.secsleft != psutil.POWER_TIME_UNLIMITED:
                    h = bat_raw.secsleft // 3600
                    m = (bat_raw.secsleft % 3600) // 60
                    tl = f"{h}h{m:02d}m"
                battery = {
                    "percent":       round(bat_raw.percent, 1),
                    "status":        status,
                    "plugged":       bat_raw.power_plugged,
                    "time_left":     tl,
                    "health":        health,
                    "full_cap_mwh":  full_cap,
                    "design_cap_mwh":design_cap,
                }

            with _state_lock:
                _state["sys"] = {
                    "cpu_pct":  round(cpu_pct, 1),
                    "cpu_freq": round(cpu_freq.current/1000, 2) if cpu_freq else None,
                    "cpu_cores":   psutil.cpu_count(logical=False),
                    "cpu_threads": psutil.cpu_count(logical=True),
                    "ram_pct":  round(ram.percent, 1),
                    "ram_used": round(ram.used/1024**3, 2),
                    "ram_total":round(ram.total/1024**3, 2),
                    "disk_act": disk_act,
                    "disk_r":   disk_r,
                    "disk_w":   disk_w,
                    "net_down": net_down,
                    "net_up":   net_up,
                    "net_iface":net_iface,
                    "battery":  battery,
                }
        except Exception as e:
            print(f"[psutil error] {e}")
        time.sleep(1.5)

# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE INFO — đầy đủ tất cả sensor từ LHM
# ══════════════════════════════════════════════════════════════════════════════
def collect_hw_sensors(node, result=None, path=""):
    """Duyệt đệ quy, chỉ lấy Fan RPM và SMART storage info."""
    if result is None:
        result = []
    val  = node.get("Value","")
    text = node.get("Text","")
    if val and val.strip() not in ("-","") and text:
        v = val.strip()
        vl = v.lower()
        tl = text.lower()
        # Fan RPM
        if "rpm" in vl:
            result.append({"name": text, "value": v, "type": "Fan"})
        # SMART: Power On Hours
        elif any(x in tl for x in ["power-on hours","power on hours","poweredonhours"]):
            result.append({"name": text, "value": v, "type": "PowerOnHours"})
        # SMART: Power Cycle Count / Power On Count
        elif any(x in tl for x in ["power cycle","power-cycle","power on count","powercycle","start/stop"]):
            result.append({"name": text, "value": v, "type": "PowerOnCount"})
    for child in node.get("Children", []):
        collect_hw_sensors(child, result, path)
    return result

def get_system_info():
    """Trả về tên máy tính, tên mainboard, tổng RAM."""
    computer = platform.node()
    board_name = None
    board_maker = None

    # Thử đọc mainboard qua WMI
    try:
        import wmi as _wmi
        w = _wmi.WMI()
        boards = w.Win32_BaseBoard()
        if boards:
            board_name  = boards[0].Product or None
            board_maker = boards[0].Manufacturer or None
    except Exception:
        pass

    # Fallback: đọc từ LHM root node
    if not board_name:
        try:
            req = urllib.request.Request(LHM_URL)
            req.add_header("User-Agent","SystemMonitor/1.0")
            with urllib.request.urlopen(req, timeout=3) as resp:
                root = json.loads(resp.read().decode())
            computers = root.get("Children",[])
            if computers:
                computer = computers[0].get("Text", computer)
        except Exception:
            pass

    ram = psutil.virtual_memory()
    ram_total = round(ram.total / 1024**3, 1)
    ram_slots = None
    try:
        import wmi as _wmi
        w = _wmi.WMI()
        sticks = w.Win32_PhysicalMemory()
        ram_slots = len(sticks)
        speeds = list({int(s.Speed) for s in sticks if s.Speed})
        ram_speed = speeds[0] if len(speeds)==1 else None
    except Exception:
        ram_speed = None

    return {
        "computer":    computer,
        "board_name":  board_name,
        "board_maker": board_maker,
        "ram_total_gb": ram_total,
        "ram_slots":   ram_slots,
        "ram_speed":   ram_speed,
    }

def build_hardware_info():
    """Trả về thông tin phần cứng: tên, loại, fan RPM, SMART storage."""
    global _lhm_hw_cache
    if not _lhm_hw_cache:
        return []
    try:
        req = urllib.request.Request(LHM_URL)
        req.add_header("User-Agent","SystemMonitor/1.0")
        with urllib.request.urlopen(req, timeout=4) as resp:
            root = json.loads(resp.read().decode())

        result = []
        computers = root.get("Children",[])
        hw_nodes  = computers[0].get("Children",[]) if computers else []

        for hw in hw_nodes:
            hw_name = hw.get("Text","")
            hw_type = classify(hw_name)
            sensors = collect_hw_sensors(hw)

            grouped = {}
            for s in sensors:
                grouped.setdefault(s["type"],[]).append(
                    {"name": s["name"], "value": s["value"]}
                )
            result.append({
                "name":    hw_name,
                "type":    hw_type,
                "sensors": grouped,
            })
        return {"hw": result, "sys": get_system_info()}
    except Exception:
        return {"hw": [], "sys": get_system_info()}

# ══════════════════════════════════════════════════════════════════════════════
# ASSEMBLE — HTTP server ghép data từ 3 thread
# ══════════════════════════════════════════════════════════════════════════════
def assemble():
    with _state_lock:
        lhm = _state["lhm"]
        nv  = _state["nvidia"]
        sys = _state["sys"]

    gpu_temp  = nv.get("temp")  or (lhm.get("gpu") or {}).get("temp")
    gpu_usage = nv.get("usage") or (lhm.get("gpu") or {}).get("usage")

    bat_base = (sys.get("battery") or {}).copy()
    bat_lhm  = lhm.get("bat") or {}
    if bat_base:
        bat_base["temp"]  = bat_lhm.get("temp")
        bat_base["watts"] = bat_lhm.get("watts")

    return {
        "ts": round(time.time()*1000),
        "cpu": {
            "usage":   sys.get("cpu_pct"),
            "temp":    lhm.get("cpu_temp"),
            "freq":    sys.get("cpu_freq"),
            "cores":   sys.get("cpu_cores"),
            "threads": sys.get("cpu_threads"),
        },
        "gpu": {
            "temp":      gpu_temp,
            "usage":     gpu_usage,
            "vram_used":  nv.get("vram_used"),
            "vram_total": nv.get("vram_total"),
        },
        "ram": {
            "usage":    sys.get("ram_pct"),
            "used_gb":  sys.get("ram_used"),
            "total_gb": sys.get("ram_total"),
            "temp":     lhm.get("ram_temp"),
        },
        "ssd": {
            "activity": sys.get("disk_act"),
            "read_mbps": sys.get("disk_r"),
            "write_mbps":sys.get("disk_w"),
            "drives":   lhm.get("storage", []),
        },
        "network": {
            "down_mbps": sys.get("net_down"),
            "up_mbps":   sys.get("net_up"),
            "iface":     sys.get("net_iface"),
        },
        "battery":  bat_base or None,
        "platform": platform.system(),
        "lhm_ok":   lhm.get("ok", False),
    }

# ── HTTP ──────────────────────────────────────────────────────────────────────
class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True   # threads tự tắt khi server dừng
    allow_reuse_address = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/stats":
            try:
                body = json.dumps(assemble()).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[http] {e}"); self.send_response(500); self.end_headers()
        elif p == "/api/debug":
            try:
                body = json.dumps(
                    [{"name":h["name"],"type":h["type"],
                      "temps":h["temps"],"loads":h["loads"]}
                     for h in _lhm_hw_cache],
                    indent=2, ensure_ascii=False).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[debug] {e}"); self.send_response(500); self.end_headers()
        elif p == "/api/hardware":
            try:
                hw_info = build_hardware_info()
                body = json.dumps(hw_info, ensure_ascii=False).encode()
                self.send_response(200); self._cors()
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                print(f"[hardware] {e}"); self.send_response(500); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for target in [lhm_thread, nvidia_thread, psutil_thread]:
        threading.Thread(target=target, daemon=True).start()

    time.sleep(1.5)   # cho threads khởi động
    print(f"\n✅  API  : http://localhost:5000/api/stats")
    print(f"   Debug: http://localhost:5000/api/debug")
    print("   Mở index.html trong trình duyệt.\n")
    try:
        ThreadingHTTPServer(("localhost", 5000), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer dừng.")
