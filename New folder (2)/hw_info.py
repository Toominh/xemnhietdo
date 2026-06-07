#!/usr/bin/env python3
"""
Hardware Info Server
Đọc thông tin phần cứng qua WMI và psutil
Chạy: python hw_info.py → mở http://localhost:5100
"""
import json, platform, subprocess, threading, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

try:
    import psutil
except ImportError:
    print("pip install psutil"); exit(1)

IS_WIN = platform.system() == "Windows"

# ── WMI helper ────────────────────────────────────────────────────────────────
def wmi_query(cls, ns="root\\cimv2", fields=None):
    if not IS_WIN:
        return []
    try:
        import wmi as _wmi
        w = _wmi.WMI(namespace=ns)
        rows = getattr(w, cls)()
        if not fields:
            return rows
        return [{f: getattr(r, f, None) for f in fields} for r in rows]
    except Exception:
        return []

def safe(v, default="N/A"):
    if v is None or str(v).strip() in ("", "None", "0"):
        return default
    return str(v).strip()

def mb(v):
    try:    return round(int(v) / 1024**2, 1)
    except: return None

def gb(v):
    try:    return round(int(v) / 1024**3, 2)
    except: return None

# ── Collectors ────────────────────────────────────────────────────────────────
def get_cpu():
    rows = wmi_query("Win32_Processor",
        fields=["Name","Manufacturer","MaxClockSpeed","NumberOfCores",
                "NumberOfLogicalProcessors","L2CacheSize","L3CacheSize",
                "Architecture","Caption"])
    cpu_freq = psutil.cpu_freq()
    result = []
    for r in rows:
        arch_map = {"0":"x86","1":"MIPS","2":"Alpha","3":"PowerPC",
                    "5":"ARM","6":"ia64","9":"x64"}
        arch = arch_map.get(safe(r.get("Architecture")), safe(r.get("Architecture")))
        result.append({
            "name":         safe(r.get("Name")),
            "manufacturer": safe(r.get("Manufacturer")),
            "cores":        safe(r.get("NumberOfCores")),
            "threads":      safe(r.get("NumberOfLogicalProcessors")),
            "max_mhz":      safe(r.get("MaxClockSpeed")),
            "cur_ghz":      round(cpu_freq.current/1000, 2) if cpu_freq else None,
            "l2_kb":        safe(r.get("L2CacheSize")),
            "l3_kb":        safe(r.get("L3CacheSize")),
            "arch":         arch,
            "usage_pct":    psutil.cpu_percent(interval=0.3),
        })
    if not result:
        freq = psutil.cpu_freq()
        result.append({
            "name":     platform.processor() or "CPU",
            "cores":    str(psutil.cpu_count(logical=False)),
            "threads":  str(psutil.cpu_count(logical=True)),
            "cur_ghz":  round(freq.current/1000,2) if freq else None,
            "usage_pct":psutil.cpu_percent(interval=0.3),
        })
    return result

def get_gpu():
    rows = wmi_query("Win32_VideoController",
        fields=["Name","AdapterRAM","DriverVersion","VideoProcessor",
                "CurrentHorizontalResolution","CurrentVerticalResolution",
                "CurrentRefreshRate","VideoModeDescription"])
    result = []
    for r in rows:
        name = safe(r.get("Name"))
        if not name or name == "N/A": continue
        vram = gb(r.get("AdapterRAM"))
        res_w = safe(r.get("CurrentHorizontalResolution"), "")
        res_h = safe(r.get("CurrentVerticalResolution"), "")
        res = f"{res_w}×{res_h}" if res_w and res_h else "N/A"
        result.append({
            "name":    name,
            "vram_gb": vram,
            "driver":  safe(r.get("DriverVersion")),
            "resolution": res,
            "refresh": safe(r.get("CurrentRefreshRate")),
        })
    return result

def get_ram():
    slots = wmi_query("Win32_PhysicalMemory",
        fields=["BankLabel","DeviceLocator","Capacity","Speed",
                "Manufacturer","PartNumber","MemoryType","FormFactor"])
    mem = psutil.virtual_memory()
    type_map = {"0":"Unknown","1":"Other","2":"DRAM","3":"Sync DRAM",
                "4":"Cache DRAM","5":"EDO","20":"DDR","21":"DDR2",
                "22":"DDR2 FB-DIMM","24":"DDR3","26":"DDR4","34":"DDR5"}
    form_map = {"0":"Unknown","1":"Other","2":"SIP","3":"DIP","4":"ZIP",
                "5":"SOJ","7":"SIMM","8":"DIMM","9":"Micro-DIMM",
                "12":"SODIMM","13":"SRIMM","14":"SMBUS"}
    sticks = []
    for s in slots:
        cap = gb(s.get("Capacity"))
        mtype = type_map.get(safe(s.get("MemoryType"),"0"), "DDR")
        form  = form_map.get(safe(s.get("FormFactor"),"0"),  "DIMM")
        sticks.append({
            "slot":     safe(s.get("DeviceLocator")),
            "bank":     safe(s.get("BankLabel")),
            "size_gb":  cap,
            "speed_mhz":safe(s.get("Speed")),
            "type":     mtype,
            "form":     form,
            "maker":    safe(s.get("Manufacturer")),
            "part":     safe(s.get("PartNumber")),
        })
    return {
        "sticks":    sticks,
        "total_gb":  round(mem.total/1024**3, 2),
        "used_gb":   round(mem.used/1024**3,  2),
        "usage_pct": mem.percent,
    }

def get_storage():
    rows = wmi_query("Win32_DiskDrive",
        fields=["Model","Size","MediaType","InterfaceType",
                "SerialNumber","FirmwareRevision","Partitions"])
    # SMART via LHM HTTP (optional)
    smart = {}
    try:
        with urllib.request.urlopen("http://localhost:8085/data.json", timeout=2) as r:
            root = json.loads(r.read().decode())
        def walk(node, parent=""):
            n = node.get("Text","")
            v = node.get("Value","")
            vl = v.lower()
            nl = n.lower()
            if any(x in nl for x in ["power-on hours","power on hours"]):
                try: smart[parent] = smart.get(parent,{}); smart[parent]["hours"] = int(float(v.split()[0]))
                except: pass
            if any(x in nl for x in ["power cycle","power on count","start/stop"]):
                try: smart[parent] = smart.get(parent,{}); smart[parent]["count"] = int(float(v.split()[0]))
                except: pass
            for c in node.get("Children",[]):
                walk(c, parent or n)
        for hw in (root.get("Children",[{}])[0].get("Children",[])):
            walk(hw, hw.get("Text",""))
    except Exception:
        pass

    result = []
    for r in rows:
        model = safe(r.get("Model"))
        size  = gb(r.get("Size"))
        iface = safe(r.get("InterfaceType"))
        media = safe(r.get("MediaType"))
        s = smart.get(model, {})
        result.append({
            "model":     model,
            "size_gb":   size,
            "interface": iface,
            "media":     media,
            "serial":    safe(r.get("SerialNumber")),
            "firmware":  safe(r.get("FirmwareRevision")),
            "partitions":safe(r.get("Partitions")),
            "hours":     s.get("hours"),
            "cycles":    s.get("count"),
        })
    # Disk usage by mountpoint
    usage = {}
    for p in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(p.mountpoint)
            usage[p.device.rstrip("\\").upper()] = {
                "mount": p.mountpoint, "fs": p.fstype,
                "total_gb": round(u.total/1024**3,2),
                "used_gb":  round(u.used/1024**3,2),
                "pct":      u.percent,
            }
        except Exception:
            pass
    return {"drives": result, "usage": usage}

def get_network():
    nics = wmi_query("Win32_NetworkAdapter",
        fields=["Name","MACAddress","AdapterType","Speed",
                "Manufacturer","NetConnectionID","NetEnabled"])
    result = []
    for n in nics:
        if not n.get("MACAddress"): continue
        nm = safe(n.get("Name"))
        if not nm or nm=="N/A": continue
        nl = nm.lower()
        if any(x in nl for x in ["virtual","vmware","hyper-v","loopback","tunnel",
                                   "teredo","isatap","miniport","wan"]):
            continue
        kind = "WiFi"    if any(x in nl for x in ["wi-fi","wireless","wlan","802.11"]) else \
               "Bluetooth" if "bluetooth" in nl else "LAN"
        spd = n.get("Speed")
        spd_str = f"{int(spd)//1_000_000} Mbps" if spd and str(spd).isdigit() else "N/A"
        result.append({
            "name":    nm,
            "mac":     safe(n.get("MACAddress")),
            "type":    kind,
            "maker":   safe(n.get("Manufacturer")),
            "conn_id": safe(n.get("NetConnectionID")),
            "enabled": n.get("NetEnabled"),
            "speed":   spd_str,
        })
    return result

def get_sound():
    rows = wmi_query("Win32_SoundDevice",
        fields=["Name","Manufacturer","Status","DeviceID"])
    result = []
    for r in rows:
        nm = safe(r.get("Name"))
        if not nm or nm=="N/A": continue
        result.append({
            "name":   nm,
            "maker":  safe(r.get("Manufacturer")),
            "status": safe(r.get("Status")),
        })
    return result

def get_battery():
    bat = psutil.sensors_battery()
    if not bat:
        rows = wmi_query("Win32_Battery",
            fields=["Name","EstimatedChargeRemaining","BatteryStatus",
                    "EstimatedRunTime","FullChargeCapacity","DesignCapacity"])
        if rows:
            r = rows[0]
            return {
                "percent": safe(r.get("EstimatedChargeRemaining")),
                "name":    safe(r.get("Name")),
            }
        return None
    status = "Đang sạc" if bat.power_plugged and bat.percent<100 else \
             "Đầy"      if bat.power_plugged else "Đang xả"
    tl = None
    if bat.secsleft and bat.secsleft>0 and bat.secsleft!=psutil.POWER_TIME_UNLIMITED:
        h,m = bat.secsleft//3600, (bat.secsleft%3600)//60
        tl = f"{h}h {m:02d}m"
    # Health via WMI
    health = None
    try:
        import wmi as _wmi
        w = _wmi.WMI(namespace="root\\wmi")
        full   = w.BatteryFullChargedCapacity()[0].FullChargedCapacity
        design = w.BatteryStaticData()[0].DesignedCapacity
        if design>0: health = round(full/design*100,1)
    except Exception:
        pass
    return {
        "percent":  round(bat.percent,1),
        "status":   status,
        "plugged":  bat.power_plugged,
        "time_left":tl,
        "health":   health,
    }

def get_board():
    brd = wmi_query("Win32_BaseBoard",
        fields=["Manufacturer","Product","Version","SerialNumber"])
    comp= wmi_query("Win32_ComputerSystem",
        fields=["Name","Manufacturer","Model","TotalPhysicalMemory","NumberOfProcessors"])
    bios= wmi_query("Win32_BIOS",
        fields=["Manufacturer","Name","Version","ReleaseDate","SMBIOSBIOSVersion"])
    result = {}
    if brd:
        r=brd[0]
        result["board_maker"]  = safe(r.get("Manufacturer"))
        result["board_name"]   = safe(r.get("Product"))
        result["board_ver"]    = safe(r.get("Version"))
        result["board_serial"] = safe(r.get("SerialNumber"))
    if comp:
        r=comp[0]
        result["computer"]   = safe(r.get("Name"))
        result["sys_maker"]  = safe(r.get("Manufacturer"))
        result["sys_model"]  = safe(r.get("Model"))
    if bios:
        r=bios[0]
        result["bios_maker"] = safe(r.get("Manufacturer"))
        result["bios_name"]  = safe(r.get("SMBIOSBIOSVersion"))
        rd = safe(r.get("ReleaseDate"),"")
        if len(rd)>=8:
            result["bios_date"] = f"{rd[6:8]}/{rd[4:6]}/{rd[:4]}"
        else:
            result["bios_date"] = rd
    return result

def collect_all():
    return {
        "cpu":     get_cpu(),
        "gpu":     get_gpu(),
        "ram":     get_ram(),
        "storage": get_storage(),
        "network": get_network(),
        "sound":   get_sound(),
        "battery": get_battery(),
        "board":   get_board(),
        "os":      {
            "name":    platform.system(),
            "version": platform.version(),
            "release": platform.release(),
            "machine": platform.machine(),
            "node":    platform.node(),
        }
    }

# ── HTTP ──────────────────────────────────────────────────────────────────────
_cache = {}
_lock  = threading.Lock()

def refresh():
    global _cache
    d = collect_all()
    with _lock:
        _cache = d

class ThreadHTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

HTML_FILE = None  # đường dẫn file html

class H(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        from urllib.parse import urlparse
        p = urlparse(self.path).path
        if p == "/api/hw":
            with _lock: body = json.dumps(_cache, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif p == "/" or p == "/index.html":
            import os
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hw_info.html")
            try:
                body = open(html_path,"rb").read()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

if __name__ == "__main__":
    print("="*55)
    print("  Hardware Info Server")
    print("="*55)
    print("Đang thu thập thông tin phần cứng...")
    refresh()
    print("✅  Xong!")
    threading.Thread(target=lambda: (
        __import__("time").sleep(30), refresh()
    ), daemon=True).start()
    HOST, PORT = "localhost", 5100
    print(f"\n✅  Mở trình duyệt: http://{HOST}:{PORT}")
    print("   Ctrl+C để dừng.\n")
    try:
        ThreadHTTP((HOST,PORT),H).serve_forever()
    except KeyboardInterrupt:
        print("Dừng.")
