"""
Hardware Info Server - Tương tự CPU-Z / Speccy
Chạy: python server.py  ->  mở http://localhost:7070
"""

import json, os, re, platform, subprocess, sys, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── Cài thư viện nếu thiếu ─────────────────────────────────
def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

for pkg in ["psutil", "py-cpuinfo", "wmi; sys_platform=='win32'"]:
    try:
        __import__(pkg.split(";")[0].strip().replace("-","_"))
    except ImportError:
        print(f"Đang cài {pkg}...")
        install(pkg)

import psutil
import cpuinfo

try:
    import wmi
    WMI = wmi.WMI()
except:
    WMI = None

# Kết nối OpenHardwareMonitor WMI — thử trực tiếp, không cần check file exe
# OHM phải đang chạy và server phải chạy với quyền Administrator
WMI_TEMP = None
try:
    import wmi as _wmi
    _ohm = _wmi.WMI(namespace="root\\OpenHardwareMonitor")
    # Thử query thực sự để xác nhận kết nối thành công
    _test = list(_ohm.Sensor())
    WMI_TEMP = _ohm
    print(f"[OK] OpenHardwareMonitor WMI: tìm thấy {len(_test)} cảm biến")
except Exception as e:
    print(f"[WARN] Không kết nối được OpenHardwareMonitor WMI: {e}")

# ── Helpers ────────────────────────────────────────────────
def safe(fn, default="N/A"):
    try: return fn()
    except: return default

def bytes2str(n):
    if n is None: return "N/A"
    for u in ["B","KB","MB","GB","TB"]:
        if abs(n) < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

def mhz(v):
    if v is None: return "N/A"
    return f"{v:,.0f} MHz" if v < 10000 else f"{v/1000:.2f} GHz"

def pct(v): return f"{v:.1f}%" if v is not None else "N/A"

# ── Thu thập dữ liệu ───────────────────────────────────────
def get_cpu():
    info = cpuinfo.get_cpu_info()
    freq = psutil.cpu_freq(percpu=False)
    freq_per = safe(lambda: psutil.cpu_freq(percpu=True))
    per_pct = psutil.cpu_percent(interval=0.3, percpu=True)
    
    # Cache sizes từ cpuinfo
    cache = {}
    for k in ["l1_data_cache_size","l1_instruction_cache_size","l2_cache_size","l3_cache_size"]:
        v = info.get(k)
        if v: cache[k] = v

    # Voltage / TDP qua WMI nếu có
    voltage = "N/A"
    tdp = "N/A"
    cpu_name_wmi = info.get("brand_raw","N/A")
    if WMI:
        try:
            for p in WMI.Win32_Processor():
                cpu_name_wmi = p.Name.strip()
                voltage = f"{p.CurrentVoltage / 10:.1f} V" if p.CurrentVoltage else "N/A"
                tdp = f"{p.DesignedCapacity} W" if hasattr(p,'DesignedCapacity') and p.DesignedCapacity else "N/A"
        except: pass

    return {
        "name": cpu_name_wmi,
        "arch": info.get("arch","N/A"),
        "bits": info.get("bits","N/A"),
        "family": info.get("family","N/A"),
        "model_id": info.get("model","N/A"),
        "stepping": info.get("stepping","N/A"),
        "vendor": info.get("vendor_id_raw","N/A"),
        "hz_base": mhz(freq.min if freq else None),
        "hz_current": mhz(freq.current if freq else None),
        "hz_max": mhz(freq.max if freq else None),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "usage_total": pct(psutil.cpu_percent(interval=0)),
        "usage_per": [pct(p) for p in per_pct],
        "freq_per": [mhz(f.current) for f in freq_per] if freq_per else [],
        "cache_l1d": cache.get("l1_data_cache_size","N/A"),
        "cache_l1i": cache.get("l1_instruction_cache_size","N/A"),
        "cache_l2": cache.get("l2_cache_size","N/A"),
        "cache_l3": cache.get("l3_cache_size","N/A"),
        "flags": sorted(info.get("flags",[]))[:40],
        "voltage": voltage,
        "tdp": tdp,
    }

def get_ram():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    
    sticks = []
    if WMI:
        try:
            for m in WMI.Win32_PhysicalMemory():
                sticks.append({
                    "tag": safe(lambda: m.Tag, "N/A"),
                    "manufacturer": safe(lambda: m.Manufacturer or "N/A"),
                    "capacity": bytes2str(int(m.Capacity)) if m.Capacity else "N/A",
                    "speed": f"{m.Speed} MHz" if m.Speed else "N/A",
                    "type": safe(lambda: {20:"DDR",21:"DDR2",24:"DDR3",26:"DDR4",34:"DDR5"}.get(m.MemoryType,"DDR"), "N/A"),
                    "slot": safe(lambda: m.DeviceLocator, "N/A"),
                    "part": safe(lambda: m.PartNumber.strip() or "N/A"),
                    "serial": safe(lambda: m.SerialNumber.strip() or "N/A"),
                    "voltage": safe(lambda: f"{m.ConfiguredVoltage / 1000:.2f} V" if m.ConfiguredVoltage else "N/A"),
                    "form": safe(lambda: {8:"DIMM",12:"SO-DIMM",13:"SO-DIMM",15:"FB-DIMM"}.get(m.FormFactor,"N/A")),
                })
        except: pass

    return {
        "total": bytes2str(vm.total),
        "available": bytes2str(vm.available),
        "used": bytes2str(vm.used),
        "percent": pct(vm.percent),
        "swap_total": bytes2str(sw.total),
        "swap_used": bytes2str(sw.used),
        "swap_pct": pct(sw.percent),
        "sticks": sticks,
        "channels": len(sticks) if sticks else "N/A",
    }

def get_disks():
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            io = psutil.disk_io_counters(perdisk=True)
            dev = part.device.replace("\\\\.\\",'').rstrip("\\")
            dev_io = io.get(dev, None) if io else None
            
            model = "N/A"
            disk_type = "N/A"
            serial = "N/A"
            size_gb = "N/A"
            interface = "N/A"
            
            if WMI:
                try:
                    for d in WMI.Win32_DiskDrive():
                        model = d.Model or "N/A"
                        size_gb = bytes2str(int(d.Size)) if d.Size else "N/A"
                        serial = d.SerialNumber.strip() if d.SerialNumber else "N/A"
                        interface = d.InterfaceType or "N/A"
                        break
                except: pass

            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": bytes2str(usage.total),
                "used": bytes2str(usage.used),
                "free": bytes2str(usage.free),
                "percent": pct(usage.percent),
                "model": model,
                "serial": serial,
                "interface": interface,
                "read_bytes": bytes2str(dev_io.read_bytes) if dev_io else "N/A",
                "write_bytes": bytes2str(dev_io.write_bytes) if dev_io else "N/A",
            })
        except: pass
    return disks

def get_gpu():
    gpus = []
    if WMI:
        try:
            for g in WMI.Win32_VideoController():
                gpus.append({
                    "name": g.Name or "N/A",
                    "vram": bytes2str(int(g.AdapterRAM)) if g.AdapterRAM else "N/A",
                    "driver": g.DriverVersion or "N/A",
                    "driver_date": safe(lambda: g.DriverDate[:8] if g.DriverDate else "N/A"),
                    "resolution": f"{g.CurrentHorizontalResolution}×{g.CurrentVerticalResolution}" if g.CurrentHorizontalResolution else "N/A",
                    "refresh": f"{g.CurrentRefreshRate} Hz" if g.CurrentRefreshRate else "N/A",
                    "color_depth": f"{g.CurrentBitsPerPixel} bit" if g.CurrentBitsPerPixel else "N/A",
                    "status": g.Status or "N/A",
                })
        except: pass
    return gpus

def get_motherboard():
    data = {"board": {}, "bios": {}, "system": {}}
    if WMI:
        try:
            for b in WMI.Win32_BaseBoard():
                data["board"] = {
                    "manufacturer": b.Manufacturer or "N/A",
                    "product": b.Product or "N/A",
                    "version": b.Version or "N/A",
                    "serial": b.SerialNumber or "N/A",
                }
            for b in WMI.Win32_BIOS():
                data["bios"] = {
                    "manufacturer": b.Manufacturer or "N/A",
                    "version": b.SMBIOSBIOSVersion or b.Version or "N/A",
                    "date": safe(lambda: b.ReleaseDate[:8] if b.ReleaseDate else "N/A"),
                    "caption": b.Caption or "N/A",
                }
            for s in WMI.Win32_ComputerSystem():
                data["system"] = {
                    "name": s.Name or "N/A",
                    "manufacturer": s.Manufacturer or "N/A",
                    "model": s.Model or "N/A",
                    "type": s.SystemType or "N/A",
                    "total_ram": bytes2str(int(s.TotalPhysicalMemory)) if s.TotalPhysicalMemory else "N/A",
                }
        except: pass
    return data

def get_os():
    up = time.time() - psutil.boot_time()
    h, r = divmod(int(up), 3600)
    m, s = divmod(r, 60)
    return {
        "name": platform.system(),
        "version": platform.version()[:60],
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor()[:60],
        "node": platform.node(),
        "python": sys.version.split()[0],
        "boot_time": datetime.fromtimestamp(psutil.boot_time()).strftime("%d/%m/%Y %H:%M"),
        "uptime": f"{h}h {m}m {s}s",
        "users": [u.name for u in psutil.users()],
    }

def get_network():
    nets = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    io = psutil.net_io_counters(pernic=True)
    for name, stat in stats.items():
        addr_list = addrs.get(name, [])
        ipv4 = next((a.address for a in addr_list if a.family.name == "AF_INET"), "N/A")
        ipv6 = next((a.address for a in addr_list if a.family.name == "AF_INET6"), "N/A")
        mac  = next((a.address for a in addr_list if a.family.name in ("AF_LINK","AF_PACKET")), "N/A")
        nic_io = io.get(name)
        nets.append({
            "name": name,
            "up": stat.isup,
            "speed": f"{stat.speed} Mbps" if stat.speed else "N/A",
            "mtu": stat.mtu,
            "ipv4": ipv4,
            "ipv6": ipv6[:30] + "..." if len(ipv6) > 30 else ipv6,
            "mac": mac,
            "sent": bytes2str(nic_io.bytes_sent) if nic_io else "N/A",
            "recv": bytes2str(nic_io.bytes_recv) if nic_io else "N/A",
            "duplex": stat.duplex.name if hasattr(stat,'duplex') else "N/A",
        })
    return nets

def get_temps():
    temps = {}

    # psutil (thường chỉ có trên Linux)
    try:
        raw = psutil.sensors_temperatures()
        if raw:
            for name, entries in raw.items():
                temps[name] = [{"label": e.label or name, "current": e.current,
                                "high": e.high, "critical": e.critical} for e in entries]
    except: pass

    # OpenHardwareMonitor qua WMI
    if WMI_TEMP:
        try:
            # Lấy tất cả hardware để map identifier -> tên đẹp
            hw_map = {}
            try:
                for hw in WMI_TEMP.Hardware():
                    hw_map[hw.Identifier] = hw.Name
            except: pass

            for sensor in WMI_TEMP.Sensor():
                try:
                    stype  = sensor.SensorType   # Temperature, Load, Clock, Fan, Voltage, Power...
                    sname  = sensor.Name
                    sval   = sensor.Value
                    sident = sensor.Identifier   # /intelcpu/0/temperature/0
                    sparent= sensor.Parent        # /intelcpu/0

                    # Tên danh mục: ưu tiên tên hardware thật
                    cat_name = hw_map.get(sparent, sparent.split("/")[-1] if "/" in sparent else sparent)

                    # Nhóm theo "TenHardware — SensorType"
                    key = f"{cat_name}"

                    if key not in temps:
                        temps[key] = []

                    temps[key].append({
                        "label":    f"[{stype}] {sname}",
                        "current":  sval,
                        "high":     None,
                        "critical": None,
                        "type":     stype,
                        "id":       sident,
                    })
                except: pass
        except Exception as e:
            temps["OHM_ERROR"] = [{"label": str(e), "current": None, "high": None, "critical": None}]

    return temps

def get_battery():
    try:
        b = psutil.sensors_battery()
        if b is None: return None
        tt = b.secsleft
        if tt == psutil.POWER_TIME_UNLIMITED: time_str = "Không giới hạn (đang sạc)"
        elif tt == psutil.POWER_TIME_UNKNOWN: time_str = "Đang tính..."
        else:
            h, r = divmod(int(tt), 3600); m = r // 60
            time_str = f"{h}h {m}m"
        return {
            "percent": round(b.percent, 1),
            "charging": b.power_plugged,
            "time_left": time_str,
        }
    except: return None

def get_processes():
    procs = []
    for p in sorted(psutil.process_iter(['pid','name','cpu_percent','memory_percent','status']),
                    key=lambda p: p.info.get('cpu_percent') or 0, reverse=True)[:20]:
        i = p.info
        procs.append({
            "pid": i['pid'],
            "name": i['name'] or "N/A",
            "cpu": pct(i.get('cpu_percent')),
            "mem": pct(i.get('memory_percent')),
            "status": i.get('status','N/A'),
        })
    return procs

def collect_all():
    t0 = time.time()
    data = {
        "cpu": get_cpu(),
        "ram": get_ram(),
        "disks": get_disks(),
        "gpu": get_gpu(),
        "motherboard": get_motherboard(),
        "os": get_os(),
        "network": get_network(),
        "temps": get_temps(),
        "battery": get_battery(),
        "processes": get_processes(),
        "scan_time": f"{(time.time()-t0)*1000:.0f}ms",
        "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    return data

# ── HTTP Server ─────────────────────────────────────────────
HTML_FILE = os.path.join(os.path.dirname(__file__), "index.html")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass   # tắt log request

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open(HTML_FILE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/hardware":
            data = collect_all()
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin","*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404); self.end_headers()

PORT = 7070
if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n{'='*50}")
    print(f"  💻  Hardware Info đang chạy tại: {url}")
    print(f"  Nhấn Ctrl+C để dừng")
    print(f"{'='*50}\n")
    # Tự mở trình duyệt sau 1 giây
    def open_browser():
        time.sleep(1)
        import webbrowser
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng server.")
