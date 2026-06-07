#!/usr/bin/env python3
"""
System Monitor — All-in-One Server
Cổng 5000:
  /              → Dashboard nhiệt độ realtime
  /hardware      → Trang thông tin phần cứng
  /api/stats     → JSON nhiệt độ + hoạt động
  /api/hw        → JSON thông tin phần cứng
  /api/hardware  → JSON LHM hardware list
  /api/debug     → JSON LHM debug
"""

import json, time, platform, subprocess, threading
import urllib.request, os, glob, socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    print("LỖI: pip install psutil"); exit(1)

IS_WIN  = platform.system() == "Windows"
LHM_URL = "http://localhost:8085/data.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

print("=" * 55)
print("  System Monitor — All-in-One")
print("=" * 55)

# ══════════════════════════════════════════════════════════════════════════════
# PHẦN 1: REALTIME MONITOR (nhiệt độ, usage)
# ══════════════════════════════════════════════════════════════════════════════

# ── nvidia-smi ────────────────────────────────────────────────────────────────
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
GPU_NVIDIA  = NVIDIA_SMI is not None
if GPU_NVIDIA:
    r = subprocess.run([NVIDIA_SMI,"--query-gpu=name","--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=3)
    print(f"✅  NVIDIA GPU : {r.stdout.strip()}")
else:
    print("ℹ️  nvidia-smi không tìm thấy (GPU qua LHM)")

# ── LHM helpers ───────────────────────────────────────────────────────────────
def parse_val(s):
    try:    return float(s.split()[0].replace(",","."))
    except: return None

def classify(name):
    n = name.lower()
    if any(x in n for x in ["ethernet","network","wi-fi","wifi","wlan","bluetooth",
        "local area","connection","realtek pcie","intel wi","killer",
        "adapter","nic ","packet scheduler","filter driver","mac layer","wfp ","qos ","ndis"]):
        return "network"
    if any(x in n for x in ["core ultra","core i","intel core","xeon","ryzen",
        "athlon","ultra 5","ultra 7","ultra 9"]):
        return "cpu"
    if any(x in n for x in ["rtx","gtx","quadro","geforce","radeon","rx ",
        "vega","intel arc","arc graphics","iris xe","uhd graphics","gpu"]):
        return "gpu"
    if any(x in n for x in ["ddr","dimm","generic memory","ram"]):
        return "ram"
    if any(x in n for x in ["nvme","ssd","samsung","sk hynix","kioxia","micron",
        "western digital","wd ","wdc","kingston","crucial","seagate",
        "toshiba","intel ssd","sabrent"]):
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
            result.append({"name":node.get("Text",""),"temp":round(v,1)})
    for c in node.get("Children",[]):
        result.extend(collect_temps(c))
    return result

def collect_loads(node):
    result = []
    val = node.get("Value","")
    if "%" in val:
        v = parse_val(val)
        if v is not None and 0 <= v <= 100:
            result.append({"name":node.get("Text",""),"load":round(v,1)})
    for c in node.get("Children",[]):
        result.extend(collect_loads(c))
    return result

def collect_hw_sensors(node, result=None):
    if result is None: result = []
    val  = node.get("Value","")
    text = node.get("Text","")
    if val and val.strip() not in ("-","") and text:
        v = val.strip(); vl = v.lower(); tl = text.lower()
        if "rpm" in vl:
            result.append({"name":text,"value":v,"type":"Fan"})
        elif any(x in tl for x in ["power-on hours","power on hours","poweredonhours"]):
            result.append({"name":text,"value":v,"type":"PowerOnHours"})
        elif any(x in tl for x in ["power cycle","power-cycle","power on count","start/stop"]):
            result.append({"name":text,"value":v,"type":"PowerOnCount"})
    for c in node.get("Children",[]):
        collect_hw_sensors(c, result)
    return result

_lhm_hw_cache = []

def fetch_lhm_raw():
    """Fetch và parse LHM JSON thành danh sách hardware."""
    global _lhm_hw_cache
    try:
        req = urllib.request.Request(LHM_URL)
        req.add_header("User-Agent","SystemMonitor/1.0")
        with urllib.request.urlopen(req, timeout=3) as resp:
            root = json.loads(resp.read().decode())
    except Exception:
        return _lhm_hw_cache

    hardware = []
    computers = root.get("Children",[])
    hw_nodes  = computers[0].get("Children",[]) if computers else []
    for hw in hw_nodes:
        hardware.append({
            "name":  hw.get("Text",""),
            "type":  classify(hw.get("Text","")),
            "temps": collect_temps(hw),
            "loads": collect_loads(hw),
            "sensors_raw": collect_hw_sensors(hw),
            "_node": hw,
        })
    _lhm_hw_cache = hardware
    return hardware

# ── Realtime parsers ───────────────────────────────────────────────────────────
def parse_cpu_temp(hw_list):
    for hw in hw_list:
        if hw["type"] != "cpu": continue
        temps = hw["temps"]
        if not temps: continue
        names = {t["name"].lower(): t["temp"] for t in temps}
        for k,v2 in names.items():
            if "package" in k: return v2
        for k,v2 in names.items():
            if "average" in k or "avg" in k: return v2
        for kw in ["tdie","tctl","cpu temperature"]:
            for k,v2 in names.items():
                if kw in k: return v2
        core_vals = [v2 for k,v2 in names.items()
                     if ("core" in k or "p-core" in k or "e-core" in k)
                     and "max" not in k and "average" not in k]
        if core_vals: return round(sum(core_vals)/len(core_vals),1)
        return temps[0]["temp"]
    return None

def parse_gpu_lhm(hw_list):
    for hw in hw_list:
        if hw["type"] != "gpu": continue
        gt = hw["temps"][0]["temp"] if hw["temps"] else None
        gu = None
        for l in hw["loads"]:
            if "core" in l["name"].lower(): gu = l["load"]; break
        return {"temp":gt,"usage":gu}
    return {"temp":None,"usage":None}

def parse_storage_temps(hw_list):
    result = []
    for hw in hw_list:
        if hw["type"]!="storage" or not hw["temps"]: continue
        result.append({"name":hw["name"],"temp":hw["temps"][0]["temp"]})
    return result

def parse_ram_temp(hw_list):
    for hw in hw_list:
        if hw["type"]=="ram" and hw["temps"]:
            return hw["temps"][0]["temp"]
    return None

def parse_battery_lhm(hw_list):
    for hw in hw_list:
        if hw["type"]!="battery": continue
        temp, watts = None, None
        for t in hw["temps"]:
            if temp is None and 0<t["temp"]<80: temp=t["temp"]
        for l in hw["loads"]:
            ln=l["name"].lower()
            if "charge rate" in ln or "watt" in ln or "power" in ln:
                watts=l["load"]
        return {"temp":temp,"watts":watts}
    return {"temp":None,"watts":None}

def build_lhm_hw_info(hw_list):
    """Trả về danh sách hardware info cho /api/hardware endpoint."""
    result = []
    for hw in hw_list:
        sensors = hw.get("sensors_raw",[])
        grouped = {}
        for s in sensors:
            grouped.setdefault(s["type"],[]).append({"name":s["name"],"value":s["value"]})
        result.append({"name":hw["name"],"type":hw["type"],"sensors":grouped})
    return result

# ── Shared state (3 threads) ───────────────────────────────────────────────────
_state = {"lhm":{},"nvidia":{},"sys":{}}
_state_lock = threading.Lock()
_lhm_ok = False

# ── Thread 1: LHM (2s) ────────────────────────────────────────────────────────
def lhm_thread():
    global _lhm_ok
    ok_logged = False
    while True:
        try:
            hw_list = fetch_lhm_raw()
            _lhm_ok = bool(hw_list)
            with _state_lock:
                _state["lhm"] = {
                    "ok":       _lhm_ok,
                    "cpu_temp": parse_cpu_temp(hw_list),
                    "gpu":      parse_gpu_lhm(hw_list),
                    "storage":  parse_storage_temps(hw_list),
                    "ram_temp": parse_ram_temp(hw_list),
                    "bat":      parse_battery_lhm(hw_list),
                    "hw_info":  build_lhm_hw_info(hw_list),
                }
            if _lhm_ok and not ok_logged:
                print("✅  LHM HTTP   : kết nối OK")
                ok_logged = True
            elif not _lhm_ok and ok_logged:
                print("⚠️  LHM mất kết nối — dùng cache")
                ok_logged = False
        except Exception as e:
            print(f"[lhm] {e}")
        time.sleep(2)

# ── Thread 2: nvidia-smi (3s) ─────────────────────────────────────────────────
def nvidia_thread():
    if not NVIDIA_SMI: return
    while True:
        try:
            r = subprocess.run(
                [NVIDIA_SMI,"--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    with _state_lock:
                        _state["nvidia"] = {
                            "temp":       float(parts[0]),
                            "usage":      float(parts[1]),
                            "vram_used":  round(float(parts[2])/1024,2),
                            "vram_total": round(float(parts[3])/1024,2),
                        }
        except Exception: pass
        time.sleep(3)

# ── Thread 3: psutil (1.5s) ───────────────────────────────────────────────────
_pd, _pt, _pn, _pnt = None, None, None, None

def get_disk_stats():
    global _pd, _pt
    try:
        c=psutil.disk_io_counters(); now=time.time()
        if _pd is None: _pd=c;_pt=now; return 0.0,0.0,0.0
        dt=now-_pt
        if dt<0.01: return 0.0,0.0,0.0
        r_mb=(c.read_bytes-_pd.read_bytes)/dt/1_048_576
        w_mb=(c.write_bytes-_pd.write_bytes)/dt/1_048_576
        _pd=c;_pt=now
        return round(r_mb,2),round(w_mb,2),round(min(100,(r_mb+w_mb)/3500*100),1)
    except: return 0.0,0.0,0.0

def get_network_stats():
    global _pn, _pnt
    try:
        now=time.time(); stats=psutil.net_io_counters(pernic=True)
        prefer=["wi-fi","wifi","wlan","wireless","wlp","wl0"]
        chosen=None
        for name,s in stats.items():
            if any(x in name.lower() for x in prefer): chosen=(name,s); break
        if chosen is None:
            for name,s in stats.items():
                nl=name.lower()
                if "lo" in nl or "loopback" in nl or "virtual" in nl: continue
                if s.bytes_recv+s.bytes_sent>0: chosen=(name,s); break
        if chosen is None: return 0.0,0.0,"N/A"
        name,cur=chosen
        if _pn is None or name not in _pn: _pn={name:cur};_pnt=now; return 0.0,0.0,name
        dt=now-_pnt
        if dt<0.01: return 0.0,0.0,name
        prev=_pn.get(name,cur)
        down=(cur.bytes_recv-prev.bytes_recv)/dt/1_048_576
        up=(cur.bytes_sent-prev.bytes_sent)/dt/1_048_576
        _pn[name]=cur;_pnt=now
        return round(max(0,down),3),round(max(0,up),3),name
    except: return 0.0,0.0,"N/A"

_bat_health_cache=(None,None,None)
_bat_health_ts=0

def get_battery_health():
    try:
        import wmi as _wmi
        w=_wmi.WMI(namespace="root\\wmi")
        full=w.BatteryFullChargedCapacity()[0].FullChargedCapacity
        design=w.BatteryStaticData()[0].DesignedCapacity
        if design>0: return round(full/design*100,1),full,design
    except: pass
    return None,None,None

def psutil_thread():
    global _bat_health_cache,_bat_health_ts
    psutil.cpu_percent(interval=None)
    while True:
        try:
            cpu_pct=psutil.cpu_percent(interval=None)
            cpu_freq=psutil.cpu_freq()
            ram=psutil.virtual_memory()
            bat_raw=psutil.sensors_battery()
            disk_r,disk_w,disk_act=get_disk_stats()
            net_down,net_up,net_iface=get_network_stats()

            now=time.time()
            if now-_bat_health_ts>60:
                _bat_health_cache=get_battery_health()
                _bat_health_ts=now
            health,full_cap,design_cap=_bat_health_cache

            battery=None
            if bat_raw:
                status=("charging" if bat_raw.power_plugged and bat_raw.percent<100
                        else "full" if bat_raw.power_plugged else "discharging")
                tl=None
                if bat_raw.secsleft and bat_raw.secsleft>0 and bat_raw.secsleft!=psutil.POWER_TIME_UNLIMITED:
                    h=bat_raw.secsleft//3600; m=(bat_raw.secsleft%3600)//60
                    tl=f"{h}h{m:02d}m"
                battery={
                    "percent":round(bat_raw.percent,1),"status":status,
                    "plugged":bat_raw.power_plugged,"time_left":tl,
                    "health":health,"full_cap_mwh":full_cap,"design_cap_mwh":design_cap,
                }

            with _state_lock:
                _state["sys"]={
                    "cpu_pct":round(cpu_pct,1),
                    "cpu_freq":round(cpu_freq.current/1000,2) if cpu_freq else None,
                    "cpu_cores":psutil.cpu_count(logical=False),
                    "cpu_threads":psutil.cpu_count(logical=True),
                    "ram_pct":round(ram.percent,1),
                    "ram_used":round(ram.used/1024**3,2),
                    "ram_total":round(ram.total/1024**3,2),
                    "disk_act":disk_act,"disk_r":disk_r,"disk_w":disk_w,
                    "net_down":net_down,"net_up":net_up,"net_iface":net_iface,
                    "battery":battery,
                }
        except Exception as e: print(f"[psutil] {e}")
        time.sleep(1.5)

def assemble_stats():
    with _state_lock:
        lhm=_state["lhm"]; nv=_state["nvidia"]; sys=_state["sys"]
    gpu_temp=nv.get("temp") or (lhm.get("gpu") or {}).get("temp")
    gpu_use =nv.get("usage") or (lhm.get("gpu") or {}).get("usage")
    bat_base=(sys.get("battery") or {}).copy()
    bat_lhm =lhm.get("bat") or {}
    if bat_base:
        bat_base["temp"]=bat_lhm.get("temp")
        bat_base["watts"]=bat_lhm.get("watts")
    return {
        "ts":round(time.time()*1000),
        "cpu":{"usage":sys.get("cpu_pct"),"temp":lhm.get("cpu_temp"),
               "freq":sys.get("cpu_freq"),"cores":sys.get("cpu_cores"),
               "threads":sys.get("cpu_threads")},
        "gpu":{"temp":gpu_temp,"usage":gpu_use,
               "vram_used":nv.get("vram_used"),"vram_total":nv.get("vram_total")},
        "ram":{"usage":sys.get("ram_pct"),"used_gb":sys.get("ram_used"),
               "total_gb":sys.get("ram_total"),"temp":lhm.get("ram_temp")},
        "ssd":{"activity":sys.get("disk_act"),"read_mbps":sys.get("disk_r"),
               "write_mbps":sys.get("disk_w"),"drives":lhm.get("storage",[])},
        "network":{"down_mbps":sys.get("net_down"),"up_mbps":sys.get("net_up"),
                   "iface":sys.get("net_iface")},
        "battery":bat_base or None,
        "platform":platform.system(),"lhm_ok":lhm.get("ok",False),
    }

# ══════════════════════════════════════════════════════════════════════════════
# PHẦN 2: HARDWARE INFO (thông tin tĩnh)
# ══════════════════════════════════════════════════════════════════════════════
def safe(v, default="N/A"):
    if v is None or str(v).strip() in ("","None","0"): return default
    return str(v).strip()

def gb(v):
    try:    return round(int(v)/1024**3,2)
    except: return None

def wmi_query(cls, ns="root\\cimv2", fields=None):
    if not IS_WIN: return []
    try:
        import wmi as _wmi
        w=_wmi.WMI(namespace=ns)
        rows=getattr(w,cls)()
        if not fields: return rows
        return [{f:getattr(r,f,None) for f in fields} for r in rows]
    except: return []

_hw_cache={}
_hw_lock=threading.Lock()
_hw_ts=0

def collect_hw():
    # CPU
    rows=wmi_query("Win32_Processor",
        fields=["Name","Manufacturer","MaxClockSpeed","NumberOfCores",
                "NumberOfLogicalProcessors","L2CacheSize","L3CacheSize","Architecture"])
    cpu_freq=psutil.cpu_freq()
    arch_map={"0":"x86","1":"MIPS","2":"Alpha","3":"PowerPC","5":"ARM","6":"ia64","9":"x64"}
    cpu=[]
    for r in rows:
        cpu.append({
            "name":safe(r.get("Name")),"manufacturer":safe(r.get("Manufacturer")),
            "cores":safe(r.get("NumberOfCores")),
            "threads":safe(r.get("NumberOfLogicalProcessors")),
            "max_mhz":safe(r.get("MaxClockSpeed")),
            "cur_ghz":round(cpu_freq.current/1000,2) if cpu_freq else None,
            "l2_kb":safe(r.get("L2CacheSize")),"l3_kb":safe(r.get("L3CacheSize")),
            "arch":arch_map.get(safe(r.get("Architecture")),safe(r.get("Architecture"))),
            "usage_pct":psutil.cpu_percent(interval=0.2),
        })
    if not cpu:
        freq=psutil.cpu_freq()
        cpu.append({"name":platform.processor()or"CPU",
                    "cores":str(psutil.cpu_count(logical=False)),
                    "threads":str(psutil.cpu_count(logical=True)),
                    "cur_ghz":round(freq.current/1000,2) if freq else None,
                    "usage_pct":psutil.cpu_percent(interval=0.2)})

    # GPU
    rows=wmi_query("Win32_VideoController",
        fields=["Name","AdapterRAM","DriverVersion",
                "CurrentHorizontalResolution","CurrentVerticalResolution","CurrentRefreshRate"])
    gpu=[]
    for r in rows:
        nm=safe(r.get("Name"))
        if not nm or nm=="N/A": continue
        rw=safe(r.get("CurrentHorizontalResolution"),"")
        rh=safe(r.get("CurrentVerticalResolution"),"")
        gpu.append({
            "name":nm,"vram_gb":gb(r.get("AdapterRAM")),
            "driver":safe(r.get("DriverVersion")),
            "resolution":f"{rw}×{rh}" if rw and rh else "N/A",
            "refresh":safe(r.get("CurrentRefreshRate")),
        })

    # RAM
    slots=wmi_query("Win32_PhysicalMemory",
        fields=["DeviceLocator","BankLabel","Capacity","Speed",
                "Manufacturer","PartNumber","MemoryType","FormFactor"])
    type_map={"20":"DDR","21":"DDR2","22":"DDR2 FB-DIMM","24":"DDR3","26":"DDR4","34":"DDR5"}
    form_map={"8":"DIMM","12":"SODIMM","13":"SRIMM"}
    mem=psutil.virtual_memory()
    sticks=[]
    for s in slots:
        sticks.append({
            "slot":safe(s.get("DeviceLocator")),"bank":safe(s.get("BankLabel")),
            "size_gb":gb(s.get("Capacity")),"speed_mhz":safe(s.get("Speed")),
            "type":type_map.get(safe(s.get("MemoryType"),"0"),"DDR"),
            "form":form_map.get(safe(s.get("FormFactor"),"0"),"DIMM"),
            "maker":safe(s.get("Manufacturer")),"part":safe(s.get("PartNumber")),
        })
    ram={"sticks":sticks,"total_gb":round(mem.total/1024**3,2),
         "used_gb":round(mem.used/1024**3,2),"usage_pct":mem.percent}

    # Storage
    rows=wmi_query("Win32_DiskDrive",
        fields=["Model","Size","MediaType","InterfaceType",
                "SerialNumber","FirmwareRevision","Partitions"])
    smart={}
    try:
        with _state_lock:
            hw_info=_state["lhm"].get("hw_info",[])
        for hw in hw_info:
            if hw["type"]=="storage":
                nm=hw["name"]
                for s in hw.get("sensors",{}).get("PowerOnHours",[]):
                    try: smart[nm]=smart.get(nm,{}); smart[nm]["hours"]=int(float(s["value"].split()[0]))
                    except: pass
                for s in hw.get("sensors",{}).get("PowerOnCount",[]):
                    try: smart[nm]=smart.get(nm,{}); smart[nm]["count"]=int(float(s["value"].split()[0]))
                    except: pass
    except: pass

    drives=[]
    for r in rows:
        model=safe(r.get("Model"))
        s_info=smart.get(model,{})
        drives.append({
            "model":model,"size_gb":gb(r.get("Size")),
            "interface":safe(r.get("InterfaceType")),"media":safe(r.get("MediaType")),
            "serial":safe(r.get("SerialNumber")),"firmware":safe(r.get("FirmwareRevision")),
            "partitions":safe(r.get("Partitions")),
            "hours":s_info.get("hours"),"cycles":s_info.get("count"),
        })
    usage={}
    for p in psutil.disk_partitions(all=False):
        try:
            u=psutil.disk_usage(p.mountpoint)
            usage[p.device.rstrip("\\").upper()]={
                "mount":p.mountpoint,"fs":p.fstype,
                "total_gb":round(u.total/1024**3,2),
                "used_gb":round(u.used/1024**3,2),"pct":u.percent,
            }
        except: pass
    storage={"drives":drives,"usage":usage}

    # Network
    rows=wmi_query("Win32_NetworkAdapter",
        fields=["Name","MACAddress","AdapterType","Speed","Manufacturer",
                "NetConnectionID","NetEnabled"])
    network=[]
    for n in rows:
        if not n.get("MACAddress"): continue
        nm=safe(n.get("Name"))
        if not nm or nm=="N/A": continue
        nl=nm.lower()
        if any(x in nl for x in ["virtual","vmware","hyper-v","loopback","tunnel",
                                   "teredo","isatap","miniport","wan"]): continue
        kind=("WiFi" if any(x in nl for x in ["wi-fi","wireless","wlan","802.11"])
              else "Bluetooth" if "bluetooth" in nl else "LAN")
        spd=n.get("Speed")
        spd_str=f"{int(spd)//1_000_000} Mbps" if spd and str(spd).isdigit() else "N/A"
        network.append({"name":nm,"mac":safe(n.get("MACAddress")),"type":kind,
                         "maker":safe(n.get("Manufacturer")),"conn_id":safe(n.get("NetConnectionID")),
                         "enabled":n.get("NetEnabled"),"speed":spd_str})

    # Sound
    rows=wmi_query("Win32_SoundDevice",fields=["Name","Manufacturer","Status"])
    sound=[]
    for r in rows:
        nm=safe(r.get("Name"))
        if nm and nm!="N/A": sound.append({"name":nm,"maker":safe(r.get("Manufacturer")),
                                            "status":safe(r.get("Status"))})

    # Battery
    bat_raw=psutil.sensors_battery()
    battery=None
    if bat_raw:
        status=("Đang sạc" if bat_raw.power_plugged and bat_raw.percent<100
                else "Đầy" if bat_raw.power_plugged else "Đang xả")
        tl=None
        if bat_raw.secsleft and bat_raw.secsleft>0 and bat_raw.secsleft!=psutil.POWER_TIME_UNLIMITED:
            h=bat_raw.secsleft//3600; m=(bat_raw.secsleft%3600)//60; tl=f"{h}h {m:02d}m"
        with _hw_lock:
            h2,fc,dc=_bat_health_cache
        battery={"percent":round(bat_raw.percent,1),"status":status,
                  "plugged":bat_raw.power_plugged,"time_left":tl,"health":h2}

    # Board
    brd=wmi_query("Win32_BaseBoard",
        fields=["Manufacturer","Product","Version","SerialNumber"])
    comp=wmi_query("Win32_ComputerSystem",
        fields=["Name","Manufacturer","Model"])
    bios=wmi_query("Win32_BIOS",
        fields=["Manufacturer","SMBIOSBIOSVersion","ReleaseDate"])
    board={}
    if brd:
        r=brd[0]; board.update({"board_maker":safe(r.get("Manufacturer")),
            "board_name":safe(r.get("Product")),"board_ver":safe(r.get("Version")),
            "board_serial":safe(r.get("SerialNumber"))})
    if comp:
        r=comp[0]; board.update({"computer":safe(r.get("Name")),
            "sys_maker":safe(r.get("Manufacturer")),"sys_model":safe(r.get("Model"))})
    if bios:
        r=bios[0]; rd=safe(r.get("ReleaseDate"),"")
        board.update({"bios_maker":safe(r.get("Manufacturer")),
            "bios_name":safe(r.get("SMBIOSBIOSVersion")),
            "bios_date":f"{rd[6:8]}/{rd[4:6]}/{rd[:4]}" if len(rd)>=8 else rd})

    os_info={"name":platform.system(),"version":platform.version(),
              "release":platform.release(),"machine":platform.machine(),"node":platform.node()}

    return {"cpu":cpu,"gpu":gpu,"ram":ram,"storage":storage,
            "network":network,"sound":sound,"battery":battery,
            "board":board,"os":os_info}

def get_hw_data():
    global _hw_ts
    with _hw_lock:
        age = time.time() - _hw_ts
        if age < 30 and _hw_cache:
            return _hw_cache
    data = collect_hw()
    with _hw_lock:
        _hw_cache.update(data)
        _hw_ts = time.time()
    return data

# ══════════════════════════════════════════════════════════════════════════════
# PHẦN 3: HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════
def serve_file(path, handler):
    try:
        body = open(path,"rb").read()
        handler.send_response(200)
        ctype = "text/html; charset=utf-8" if path.endswith(".html") else "application/octet-stream"
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers(); handler.wfile.write(body)
    except FileNotFoundError:
        handler.send_response(404); handler.end_headers()
        handler.wfile.write(b"File not found")

def serve_json(data, handler):
    body = json.dumps(data, ensure_ascii=False).encode()
    handler.send_response(200)
    handler.send_header("Content-Type","application/json")
    handler.send_header("Access-Control-Allow-Origin","*")
    handler.send_header("Content-Length",str(len(body)))
    handler.end_headers(); handler.wfile.write(body)

class ThreadHTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")
        self.end_headers()
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p in ("/","/index.html"):
                serve_file(os.path.join(BASE_DIR,"index.html"), self)
            elif p in ("/hardware","/hardware/","/hw_info.html"):
                serve_file(os.path.join(BASE_DIR,"hw_info.html"), self)
            elif p == "/api/stats":
                serve_json(assemble_stats(), self)
            elif p == "/api/hw":
                serve_json(get_hw_data(), self)
            elif p == "/api/hardware":
                with _state_lock:
                    hw_info = _state["lhm"].get("hw_info",[])
                serve_json({"hw":hw_info,"sys":{
                    "computer":platform.node(),
                    "ram_total_gb":round(psutil.virtual_memory().total/1024**3,2),
                }}, self)
            elif p == "/api/debug":
                serve_json([{"name":h["name"],"type":h["type"],
                             "temps":h["temps"],"loads":h["loads"]}
                            for h in _lhm_hw_cache], self)
            else:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"Not found")
        except Exception as e:
            print(f"[http] {e}")
            self.send_response(500); self.end_headers()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    HOST, PORT = "localhost", 5000

    for target in [lhm_thread, nvidia_thread, psutil_thread]:
        threading.Thread(target=target, daemon=True).start()
    time.sleep(1.5)

    print(f"\n✅  Dashboard nhiệt độ : http://{HOST}:{PORT}/")
    print(f"✅  Thông tin phần cứng: http://{HOST}:{PORT}/hardware")
    print(f"   API stats    : http://{HOST}:{PORT}/api/stats")
    print(f"   API hardware : http://{HOST}:{PORT}/api/hw")
    print("   Ctrl+C để dừng.\n")

    try:
        ThreadHTTP((HOST,PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer dừng.")
