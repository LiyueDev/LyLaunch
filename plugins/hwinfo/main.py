"""硬件信息插件（LyLaunch 内置插件）。

点插件瓦片 → 弹出一个**横板硬件概览**（两列网格铺开，纯文字、无灰色卡片底）：
左边蓝色短标签（OS/CPU/GPU/MB/RAM/SSD），右边灰色类别名 + 白色规格。
右上角「查看详情」打开更详尽的横板视图（每个部件一段，逐条键值）。

数据用 Windows WMI（pywin32）采集，全部本地、不联网、无第三方依赖。
采集耗时（WMI 首调较慢）放在 QThread 里跑，不卡主程序界面。
"""

from __future__ import annotations

import os
import re
import time
import winreg

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from liytools.plugins.base import PluginBase
from liytools.plugins import theme
from liytools.plugins.plugin_icons import generic_icon_svg, render_plugin_svg

# 模块级单例：持有引用防 GC，重复打开只提到前台（第十章头号红线，禁止 dlg.exec()）。
_DLG = None
_DETAIL_DLG = None


# ── 主板厂商 → 中文品牌 ──────────────────────────────────────────────
_BRAND_MAP = {
    "gigabyte": "技嘉",
    "onda": "昂达",
    "colorful": "七彩虹",
    "asus": "华硕",
    "asrock": "华擎",
    "msi": "微星",
    "biostar": "映泰",
    "maxsun": "铭瑄",
    "soyo": "翔升",
    "huanan": "华南金牌",
    "intel": "英特尔",
    "amd": "AMD",
    "acer": "宏碁",
    "dell": "戴尔",
    "lenovo": "联想",
    "hp": "惠普",
    "microsoft": "微软",
    "hasee": "神舟",
    "tongfang": "同方",
    "spercent": "七彩虹",   # 部分七彩虹写 Splendid/Colorful
    "yeston": "盈通",
}
_SUFFIX_RE = re.compile(
    r"\b(technology|technologies|co\.?,?\s*ltd\.?|inc\.?|corporation|corp\.?|"
    r"company|companies|limited|holdings?|electronics|microsystems|"
    r"international|group|co)\b", re.I)


def _brand_name(raw: str) -> str:
    """把厂商英文名转成中文品牌；认不到就去掉公司后缀返回原样。"""
    if not raw:
        return "未知"
    r = raw.strip()
    low = r.lower()
    for key, cn in _BRAND_MAP.items():
        if key in low:
            return cn
    cleaned = _SUFFIX_RE.sub("", r).strip(" ,.-")
    return cleaned or r


# ── 工具函数 ───────────────────────────────────────────────────────
def _fmt_bytes(n) -> str:
    """把字节数转成「X.X GB / X MB」可读串；异常或 0 返回「未知」。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "未知"
    if n <= 0:
        return "未知"
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.0f} MB"


def _fmt_gb(n) -> str:
    """把字节数转成紧凑容量串「24GB / 8GB / 1.5GB」（整数去小数，无空格）。异常或 0 返回「未知」。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "未知"
    if n <= 0:
        return "未知"
    gb = n / (1024 ** 3)
    if gb >= 1:
        # 驱动上报的显存常是 11.9GiB 这类非整值（标称 12GB），接近整数就取整，
        # 避免概览里出现 11.9GB / 23.9GB 这种「看起来像少了显存」的怪值。
        nearest = round(gb)
        if nearest > 0 and abs(gb - nearest) <= 0.25:
            return f"{nearest}GB"
        return f"{gb:.1f}GB"
    return f"{n / (1024 ** 2):.0f}MB"


# Win32_PhysicalMemory 的 SMBIOSMemoryType / MemoryType 码 → DDR 代际名
_MEM_TYPES = {
    20: "DDR", 21: "DDR2", 22: "DDR2", 24: "DDR3", 25: "DDR3",
    26: "DDR4", 27: "DDR4", 30: "LPDDR4", 31: "LPDDR4X", 33: "LPDDR5",
    34: "DDR5", 35: "DDR5",
}


# ── 显存读取 ───────────────────────────────────────────────────────
# WMI 的 Win32_VideoController.AdapterRAM 是 32 位字段：独显显存 >2GB 时会溢出
# （例如 12GB 正好取模归零、8GB 也会归零、6GB 变负数），所以绝不能直接信它。
# 真实显存改从注册表读：显卡类子键里的 HardwareInformation.qwMemorySize 是 64 位 REG_QWORD。
_DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _read_vram_from_key(k) -> int:
    """从某个显卡注册表子项读显存字节数；优先 64 位 qwMemorySize，退回 32 位 MemorySize。取不到返回 0。"""
    import struct
    # 64 位真实显存（绝大多数现代驱动会写这一项）
    try:
        v = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
        if isinstance(v, int) and v > 0:
            return v
    except OSError:
        pass
    # 32 位 MemorySize：可能是 REG_BINARY(小端 4 字节) 或 REG_DWORD(int)
    try:
        v = winreg.QueryValueEx(k, "HardwareInformation.MemorySize")[0]
        if isinstance(v, (bytes, bytearray)) and len(v) >= 4:
            return struct.unpack("<I", bytes(v[:4]))[0]
        if isinstance(v, int) and v > 0:
            return v
    except OSError:
        pass
    return 0


def _subsys_vendor_from_text(text) -> int | None:
    """从含 SUBSYS_xxxxxxxx 的字符串（WMI PNPDeviceID / DeviceID / 注册表 MatchingDeviceId）
    提取板卡厂商 ID（Subsystem Vendor ID）。取不到返回 None。

    ⚠️ 坑（真机 bug 根因）：按 PCI 规范，Windows 硬件 ID 里 SUBSYS_ 后 8 位是
    「Subsystem Device ID(高16) + Subsystem Vendor ID(低16)」，**低 16 位才是厂商 ID**。
      AMD 官方示例  SUBSYS_0B041002 → 厂商 1002（AMD）
      索泰示例      SUBSYS_C30819DA → 厂商 19DA（索泰）
      七彩虹真机    SUBSYS_15007377 → 厂商 7377（七彩虹）
    旧版误取高 16 位（>>16），把 1500 当厂商 ID 去查表必然查不到 → 回退成 NVIDIA。
    个别 BIOS/驱动会把厂商写在高位，故两个候选都试，优先返回能命中厂商表的那个。
    """
    if not text:
        return None
    m = re.search(r"SUBSYS_([0-9A-Fa-f]{8})", str(text))
    if not m:
        return None
    val = int(m.group(1), 16)
    low = val & 0xFFFF          # 规范：低 16 位 = Subsystem Vendor ID
    high = (val >> 16) & 0xFFFF  # 兼容个别把厂商放高位的写法
    if low in _PCI_VENDOR_NAMES:
        return low
    if high in _PCI_VENDOR_NAMES:
        return high
    return low


def _gpu_registry_info(name: str) -> tuple[int, int | None]:
    """从显卡类注册表读 (真实显存字节数, SUBSYS 板卡厂商ID)；取不到对应返回 (0, None)。

    厂商 ID 的字节序解释统一交给 _subsys_vendor_from_text()（低 16 位为厂商），
    与显存同一次遍历取出，免得为厂商再开一遍注册表。
    """
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_KEY)
    except OSError:
        return (0, None)
    best_vram = 0
    best_sub = None
    try:
        idx = 0
        while True:
            try:
                sub = winreg.EnumKey(base, idx)
            except OSError:
                break
            idx += 1
            if not (len(sub) == 4 and sub.isdigit()):
                continue
            try:
                k = winreg.OpenKey(base, sub)
            except OSError:
                continue
            try:
                desc = winreg.QueryValueEx(k, "DriverDesc")[0]
            except OSError:
                desc = ""
            vram = _read_vram_from_key(k)
            # 板卡厂商：解析 MatchingDeviceId 里的 SUBSYS_xxxxxxxx 高 16 位
            sub_id = None
            try:
                mid = winreg.QueryValueEx(k, "MatchingDeviceId")[0]
                sub_id = _subsys_vendor_from_text(mid)
            except OSError:
                pass
            matched = bool(desc) and (name.lower() in desc.lower() or desc.lower() in name.lower())
            if vram and vram > 0:
                if matched:
                    try:
                        winreg.CloseKey(k)
                    except OSError:
                        pass
                    try:
                        winreg.CloseKey(base)
                    except OSError:
                        pass
                    return (vram, sub_id)
                if vram > best_vram:
                    best_vram, best_sub = vram, sub_id
            try:
                winreg.CloseKey(k)
            except OSError:
                pass
    finally:
        try:
            winreg.CloseKey(base)
        except OSError:
            pass
    return (best_vram, best_sub)


# ── 厂商 ID / 代码 → 中文品牌 ───────────────────────────────────────
# 显卡板卡品牌：SUBSYS 高 16 位 = PCI 子系统厂商 ID（AIB 板卡厂，非芯片厂）。
# 芯片厂 NVIDIA/AMD/Intel 也列进来，识别不到板卡时退回到芯片厂。
_PCI_VENDOR_NAMES = {
    # 芯片厂（识别不到板卡时退回）
    0x10DE: "NVIDIA", 0x1002: "AMD", 0x8086: "英特尔",
    # 板卡厂（AIB）：子系统厂商 ID
    0x7377: "七彩虹",          # Colorful（权威：TechPowerUp / GPU-Z / nvapi=0x7377）
    0x1458: "技嘉", 0x1462: "微星", 0x1043: "华硕",
    0x147B: "影驰",            # Galaxy
    0x19DA: "索泰",            # ZOTAC
    0x1DA2: "蓝宝石", 0x174B: "蓝宝石",  # Sapphire（两码均见）
    0x148C: "迪兰",            # PowerColor / TUL（撼讯同系）
    0x1C82: "耕升", 0x1B31: "映众",
    0x196E: "PNY", 0x14B9: "同德",
    0x1682: "讯景", 0x1EAE: "盈通",
    0x6845: "铭瑄",
}


def _chip_vendor_brand(raw: str) -> str | None:
    """把 WMI AdapterCompatibility 归一化成规范芯片厂名。

    识别不到板卡品牌（七彩虹/技嘉…）时，用它作为**保底**：只显示 NVIDIA / AMD / 英特尔，
    不把 "NVIDIA Corporation"、"Advanced Micro Devices, Inc." 这类原始长串抛到界面上。
    """
    s = (raw or "").lower()
    if "nvidia" in s:
        return "NVIDIA"
    if "advanced micro" in s or "ati technologies" in s or "amd" in s:
        return "AMD"
    if "intel" in s:
        return "英特尔"
    return None


# 显示器厂商：Win32_DesktopMonitor.MonitorManufacturer 的 3 字母 PNP 码 → 中文品牌。
_MONITOR_VENDOR = {
    "SAM": "三星", "GSM": "LG", "LGD": "LG", "DEL": "戴尔", "HWP": "惠普",
    "SNY": "索尼", "ACR": "宏碁", "AOC": "AOC", "HKC": "惠科", "BOE": "京东方",
    "AUO": "友达", "CMN": "群创", "PHL": "飞利浦", "VSC": "优派", "XMI": "小米",
    "LEN": "联想", "APP": "苹果", "BNQ": "明基", "CPQ": "康柏", "EIZ": "艺卓",
    "FUS": "富士通", "IVM": "Iiyama", "NEC": "NEC", "SHP": "夏普", "TOS": "东芝",
    "VIZ": "Vizio", "MEI": "松下", "OPT": "奥图码", "PRT": "Proview", "SAN": "三洋",
    "WDE": "Westinghouse", "ZNM": "Zenith", "MED": "Medion", "GWY": "Gateway",
    "HIT": "日立", "JVC": "JVC", "MAG": "美格", "MAX": "Maxdata", "CHR": "Christie",
    "CMV": "CMV", "ELO": "Elo", "HSD": "HannStar", "HYD": "现代", "INL": "InFocus",
    "KFC": "KFC", "LKM": "Lockmart", "TAT": "Tatung", "CPL": "Compal", "DCL": "Denver",
    "ECS": "精英", "FNI": "Funai", "GAT": "Gatestar", "GRV": "Graffiti", "HIQ": "HiQ",
    "ICL": "ICL", "ISM": "Infosource", "JTT": "Jaton", "KDS": "KDS", "LNK": "Link",
    "MTC": "MTC", "NOK": "Nokia", "ORI": "Oracle", "PBN": "ProBench", "REL": "Relisys",
    "SMC": "Samtron", "SST": "Samsung?", "TSL": "Toshiba", "UNI": "Unisys", "YMH": "Yamaha",
}

# MonitorManufacturer 有时返回完整厂商名而非 PNP 码，做一层关键词兜底
_MONITOR_ALIASES = (
    ("SAMSUNG", "三星"), ("DELL", "戴尔"), ("HEWLETT", "惠普"),
    ("PHILIPS", "飞利浦"), ("VIEWSONIC", "优派"), ("ACER", "宏碁"),
    ("BENQ", "明基"), ("LENOVO", "联想"), ("XIAOMI", "小米"),
    ("INNOCN", "联合创新"), ("KOORUI", "科睿"), ("HISENSE", "海信"),
    ("ASUS", "华硕"), ("APPLE", "苹果"), ("SONY", "索尼"),
    ("LG ELECTRONICS", "LG"), ("GOLDSTAR", "LG"), ("AOC", "AOC"),
    ("HKC", "惠科"), ("BOE", "京东方"), ("TCL", "TCL"), ("KTC", "KTC"),
)


def _monitor_brand(code: str) -> str | None:
    """PNP 3 字母码 → 中文品牌；也兼容完整厂商名（SAMSUNG / LG Electronics…）。"""
    if not code:
        return None
    c = code.strip().upper()
    if len(c) >= 3:
        hit = _MONITOR_VENDOR.get(c[:3])
        if hit:
            return hit
    for key, brand in _MONITOR_ALIASES:
        if key in c:
            return brand
    return None


# 注册表里显示器枚举键：子键名就是 PNP 码（如 SAM0E55 / LGD04D5），前 3 字母即厂商码。
_MONITOR_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"


def _monitor_codes_from_registry() -> list[str]:
    """从注册表 Enum\\DISPLAY 枚举显示器 PNP 厂商码。

    Win32_DesktopMonitor 在现代 Windows 上经常只返回一个「默认监视器」且不填
    MonitorManufacturer / PNPDeviceID，改用这里枚举更可靠。取不到返回空列表。
    """
    codes: list[str] = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MONITOR_ENUM_KEY)
    except OSError:
        return codes
    try:
        idx = 0
        while True:
            try:
                sub = winreg.EnumKey(base, idx)
            except OSError:
                break
            idx += 1
            if len(sub) >= 3 and sub[:3].isalpha():
                codes.append(sub[:3].upper())
    finally:
        try:
            winreg.CloseKey(base)
        except OSError:
            pass
    return codes


def _connect_wmi():
    """建一个 WMI 连接（调用方必须在线程里先 CoInitialize）。失败抛异常由上层兜底。"""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    return win32com.client.GetObject("winmgmts://./root/cimv2")


def _safe_query(wmi, wql):
    """执行一条 WQL，单条失败不影响其它项，返回对象列表。"""
    try:
        return list(wmi.ExecQuery(wql))
    except Exception:
        return []


# ── 硬件采集（在线程里跑）─────────────────────────────────────────
def collect_hardware():
    """采集本机硬件信息。

    返回 (sections, summary, 耗时ms)：
      sections: dict[标题 -> [(键, 值), ...]]  详细视图用
      summary:  list[(tag, 类别中文, 一句话规格)]  紧凑概览用
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    summary: list[tuple[str, str, str]] = []
    t0 = time.perf_counter()
    wmi = None
    try:
        wmi = _connect_wmi()
    except Exception:
        wmi = None

    try:
        # ── 系统 ──
        sys_rows: list[tuple[str, str]] = []
        os_caption = "未知"
        arch = ""
        if wmi is not None:
            for osi in _safe_query(wmi, "SELECT * FROM Win32_OperatingSystem"):
                os_caption = (osi.Caption or "").strip() or "未知"
                if os_caption.lower().startswith("microsoft "):
                    os_caption = os_caption[len("microsoft "):]
                arch = osi.OSArchitecture or ""
                sys_rows.append(("操作系统", os_caption + (f" {arch}" if arch else "")))
                sys_rows.append(("系统版本", osi.Version or "未知"))
                sys_rows.append(("设备名称", osi.CSName or "未知"))
                break
        try:
            sys_rows.append(("当前用户", os.getlogin()))
        except Exception:
            pass
        if sys_rows:
            sections["系统"] = sys_rows
            summary.append(("OS", "系统", f"{os_caption} {arch}".strip()))

        # ── 处理器 ──
        cpu_rows: list[tuple[str, str]] = []
        cpu_name = "未知"
        cores = threads = clk = None
        if wmi is not None:
            for cpu in _safe_query(wmi, "SELECT * FROM Win32_Processor"):
                cpu_name = (cpu.Name or "").strip() or "未知"
                cpu_rows.append(("名称", cpu_name))
                cpu_rows.append(("厂商", cpu.Manufacturer or "未知"))
                cores = cpu.NumberOfCores
                threads = cpu.NumberOfLogicalProcessors
                if cores:
                    txt = f"{cores} 核"
                    if threads:
                        txt += f" / {threads} 线程"
                    cpu_rows.append(("核心 / 线程", txt))
                clk = cpu.MaxClockSpeed
                if clk:
                    cpu_rows.append(("基准频率", f"{clk / 1000:.2f} GHz"))
                break
        if cpu_rows:
            sections["处理器"] = cpu_rows
            head = cpu_name
            if cores:
                head += f" · {cores}核{threads or ''}线程" if threads else f" · {cores}核"
            if clk:
                head += f" · {clk / 1000:.2f}GHz"
            summary.append(("CPU", "处理器", head.strip(" ·")))

        # ── 主板（厂商转中文品牌）──
        mb_rows: list[tuple[str, str]] = []
        mb_mfr = mb_prod = ""
        if wmi is not None:
            for mb in _safe_query(wmi, "SELECT * FROM Win32_BaseBoard"):
                mb_mfr = mb.Manufacturer or ""
                mb_prod = mb.Product or ""
                brand = _brand_name(mb_mfr)
                mb_rows.append(("厂商", brand or "未知"))
                mb_rows.append(("型号", mb_prod or "未知"))
                if mb.Version:
                    mb_rows.append(("版本", mb.Version))
                sn = mb.SerialNumber
                if sn and sn not in ("None", "0", ""):
                    mb_rows.append(("序列号", sn))
                break
        if mb_rows:
            sections["主板"] = mb_rows
            mb_head = f"{_brand_name(mb_mfr)} {mb_prod}".strip() or "未知"
            summary.append(("MB", "主板", mb_head))

        # ── 内存 ──
        mem_rows: list[tuple[str, str]] = []
        total = None
        if wmi is not None:
            for cs in _safe_query(wmi, "SELECT * FROM Win32_ComputerSystem"):
                total = cs.TotalPhysicalMemory
                break
        sticks: list[tuple[str, str, str, object, int]] = []  # (容量, 速度, 厂商, 类型码, 字节数)
        if wmi is not None:
            for pm in _safe_query(wmi, "SELECT * FROM Win32_PhysicalMemory"):
                # WMI 的 Capacity 在部分机器上是字符串（如 "8589934592"），统一转 int 再累加
                cap_raw = pm.Capacity or 0
                try:
                    cap_raw_int = int(cap_raw)
                except (TypeError, ValueError):
                    cap_raw_int = 0
                cap = _fmt_gb(cap_raw_int)
                spd = f"{pm.Speed} MHz" if pm.Speed else ""
                maker = pm.Manufacturer or ""
                mtype = getattr(pm, "SMBIOSMemoryType", None)
                if mtype is None:
                    mtype = getattr(pm, "MemoryType", None)
                sticks.append((cap, spd, maker, mtype, cap_raw_int))
        # 总容量以各内存条之和计（比 OS 报告的 TotalPhysicalMemory 更干净，避免 23.9GB）
        total_cap = sum(s[4] for s in sticks)
        total_str = _fmt_gb(total_cap) if total_cap else _fmt_gb(total)
        if total_str != "未知":
            mem_rows.append(("总容量", total_str))
        if sticks:
            mem_rows.append(("插槽数量", f"{len(sticks)} 条"))
            for i, (cap, spd, maker, _t, _raw) in enumerate(sticks, 1):
                mem_rows.append((f"插槽 {i}", cap + (f" · {spd}" if spd else "") + (f" · {maker}" if maker else "")))
        if mem_rows:
            sections["内存"] = mem_rows
            # 概览：24GB DDR4 3200MHz（8GB + 8GB + 8GB）
            mtype = _MEM_TYPES.get(int(sticks[0][3]), "") if sticks and sticks[0][3] is not None else ""
            speed = sticks[0][1].replace(" MHz", "MHz") if sticks and sticks[0][1] else ""
            head = total_str
            if mtype:
                head += f" {mtype}"
            if speed:
                head += f" {speed}"
            stick_str = " + ".join(s[0] for s in sticks)
            head += f"（{stick_str}）"
            summary.append(("RAM", "内存", head))

        # ── 显卡（可能有多张：核显 + 独显；过滤掉虚拟/远程显示设备）──
        # 显存：WMI 的 AdapterRAM 是 32 位字段，>2GB 会溢出归零/变负，绝不可信，
        # 一律改由 _gpu_registry_info() 读注册表 64 位真实值（顺带取 SUBSYS 板卡厂商 ID），WMI 仅作兜底。
        _VIRTUAL_GPU = ("virtual", "todesk", "remot", "rdp", "parsec",
                        "virtualbox", "vmware", "citrix", "microsoft basic display")
        gpus = _safe_query(wmi, "SELECT * FROM Win32_VideoController") if wmi is not None else []
        real_gpus = [v for v in gpus
                     if (v.Name or "").strip()
                     and not any(k in (v.Name or "").lower() for k in _VIRTUAL_GPU)]
        if not real_gpus:
            real_gpus = gpus  # 兜底：过滤后为空就退回全显示，避免整段空白
        for i, vc in enumerate(real_gpus, 1):
            rows: list[tuple[str, str]] = []
            name = (vc.Name or "").strip() or "未知"
            rows.append(("名称", name))
            # 芯片厂（NVIDIA / AMD / 英特尔）：归一化后的规范名，作为识别不到板卡品牌时的保底
            chip_vendor = _chip_vendor_brand(getattr(vc, "AdapterCompatibility", ""))
            # 真实显存：注册表 64 位 qwMemorySize（WMI AdapterRAM 是 32 位，>2GB 会溢出归零）
            vram, reg_subsys_vid = _gpu_registry_info(name)
            if not vram:
                ram = vc.AdapterRAM
                try:
                    ram = int(ram)
                except (TypeError, ValueError):
                    ram = 0
                if isinstance(ram, int) and 0 < ram <= 24 * (1024 ** 3):
                    vram = ram
            vram_ok = isinstance(vram, int) and vram > 0
            # 板卡厂商 ID：优先用「这张卡自己的」PnP 硬件 ID（多卡时不会串到别的卡），
            # 再退回注册表 MatchingDeviceId，最后才退回芯片厂 NVIDIA/AMD/Intel。
            # 注意注册表是全局遍历 + 名字模糊匹配，多显卡时不可靠，所以放在后面。
            subsys_vid = _subsys_vendor_from_text(getattr(vc, "PNPDeviceID", "")) \
                or _subsys_vendor_from_text(getattr(vc, "DeviceID", "")) \
                or reg_subsys_vid
            # 板卡品牌（七彩虹/技嘉…）优先；识别不到就保底显示规范芯片厂名 NVIDIA / AMD / 英特尔
            board = _PCI_VENDOR_NAMES.get(subsys_vid) if subsys_vid else None
            vendor = board or chip_vendor
            if vendor:
                rows.append(("厂商", vendor))
            if vram_ok:
                rows.append(("显存", _fmt_gb(vram)))
            driver = vc.DriverVersion or ""
            if driver:
                rows.append(("驱动版本", driver))
            if rows:
                title = f"显卡 {i}" if len(real_gpus) > 1 else "显卡"
                sections[title] = rows
                # 概览：NVIDIA GeForce RTX 5070 (12GB / 七彩虹)
                inner = []
                if vram_ok:
                    inner.append(_fmt_gb(vram))
                if vendor:
                    inner.append(vendor)
                head = name + (f" ({' / '.join(inner)})" if inner else "")
                if driver:
                    head += f"\n驱动 {driver}"
                tag = "GPU" if len(real_gpus) == 1 else f"GPU{i}"
                summary.append((tag, "显卡", head))

        # ── 显示器（分辨率 / 刷新率；多屏换行；与显卡区分开）──
        # 优先用 Win32_DisplayConfiguration；该 legacy 类在部分机器上属性取不到
        # （pywin32 抛 AttributeError: <unknown>.HorizontalResolution），所以用 getattr 兜底，
        # 取不到时退回 Win32_VideoController 的 CurrentHorizontalResolution 等字段（现代 Windows 更可靠）。
        disp_infos: list[tuple[int, int, int]] = []
        seen = set()

        def _collect(res_list) -> None:
            """从一组 WMI 对象里提取 (宽,高,刷新率)，去重后追加到 disp_infos。"""
            for obj in res_list:
                try:
                    w = getattr(obj, "HorizontalResolution", None) \
                        or getattr(obj, "CurrentHorizontalResolution", None)
                    h = getattr(obj, "VerticalResolution", None) \
                        or getattr(obj, "CurrentVerticalResolution", None)
                    freq = getattr(obj, "DisplayFrequency", None) \
                        or getattr(obj, "CurrentRefreshRate", None)
                except Exception:
                    w = h = freq = None
                if w and h:
                    try:
                        key = (int(w), int(h), int(freq or 0))
                    except (TypeError, ValueError):
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    disp_infos.append(key)

        if wmi is not None:
            _collect(_safe_query(wmi, "SELECT * FROM Win32_DisplayConfiguration"))
        # 兜底：DisplayConfiguration 取不到就用显卡控制器的当前分辨率
        if not disp_infos and real_gpus:
            _collect(real_gpus)

        # 显示器厂商：Win32_DesktopMonitor.MonitorManufacturer 给 3 字母 PNP 码，查表转中文品牌。
        # 与上面的分辨率按序号尽量对应（多屏时 best-effort）。
        mon_brands: list[str] = []
        if wmi is not None:
            for dm in _safe_query(wmi, "SELECT * FROM Win32_DesktopMonitor"):
                # PNPDeviceID 形如 DISPLAY\SAM0E55\... 或 MONITOR\SAM0E55\...，前 3 字母即厂商码，
                # 比 MonitorManufacturer 可靠（后者在不少机器上是空的）。
                pnp = getattr(dm, "PNPDeviceID", "") or ""
                code = ""
                m = re.search(r"(?:DISPLAY|MONITOR)\\([A-Za-z0-9]{3})", pnp)
                if m:
                    code = m.group(1).upper()
                if not code:
                    code = (getattr(dm, "MonitorManufacturer", "") or "").strip().upper()
                b = _monitor_brand(code)
                if b:
                    mon_brands.append(b)
        # 兜底：WMI 一个厂商码都没给（常见于「默认监视器」），改用注册表枚举的 PNP 码
        if not mon_brands:
            for code in _monitor_codes_from_registry():
                b = _monitor_brand(code)
                if b and b not in mon_brands:
                    mon_brands.append(b)

        # 显示器型号：Win32_PnPEntity 的 Name 常写成 "Generic Monitor (L32K2C)"。
        # 有些屏的 EDID 只给冷门 PNP 码（真机实测 XSL）且 Windows 自己也只认成
        # 「通用即插即用监视器」，品牌客观上取不到；此时至少把型号亮出来，不让这栏空白。
        # 顺带排除 ToDesk 等虚拟显示器（VDisplay00）。
        mon_models: list[str] = []
        if wmi is not None:
            for ent in _safe_query(wmi, "SELECT Name FROM Win32_PnPEntity WHERE PNPClass='Monitor'"):
                nm = (getattr(ent, "Name", "") or "").strip()
                mm = re.search(r"\(([^()]+)\)\s*$", nm)
                if not mm:
                    continue
                model = mm.group(1).strip()
                if not model or model.lower().startswith(("vdisplay", "virtual")):
                    continue
                if model not in mon_models:
                    mon_models.append(model)

        # 识别不到具体品牌时，直接用 EDID 里的型号顶到「厂商」位（其他硬件检测工具也这么显示），
        # 不额外再开一行「型号」，保持概览/详情简洁一致。
        if not mon_brands and mon_models:
            mon_brands = mon_models[:max(1, len(disp_infos))]

        if disp_infos:
            mon_rows: list[tuple[str, str]] = []
            mon_head_parts: list[str] = []
            for idx, (w, h, freq) in enumerate(disp_infos, 1):
                brand = mon_brands[idx - 1] if idx - 1 < len(mon_brands) else None
                line = f"{w} × {h}"
                if freq:
                    line += f" · {freq}Hz"
                label = "分辨率" if len(disp_infos) == 1 else f"显示器 {idx}"
                mon_rows.append((label, line))
                if brand:
                    mon_rows.append(("厂商", brand))
                # 概览行带品牌（或型号兜底）前缀
                head_line = (brand + " ") if brand else ""
                head_line += line
                mon_head_parts.append(head_line)
            sections["显示器"] = mon_rows
            headline = "\n".join(mon_head_parts) if len(mon_head_parts) > 1 else (mon_head_parts[0] if mon_head_parts else "")
            summary.append(("MON", "显示器", headline))

        # ── 存储 ──
        disk_rows: list[tuple[str, str]] = []
        disk_head_parts: list[str] = []
        if wmi is not None:
            for dd in _safe_query(wmi, "SELECT * FROM Win32_DiskDrive"):
                model = (dd.Model or "").strip() or "未知磁盘"
                size = _fmt_bytes(dd.Size)
                iface = dd.InterfaceType or ""
                disk_rows.append((model, size + (f" · {iface}" if iface else "")))
                disk_head_parts.append(f"{model} {size}")
        if disk_rows:
            sections["存储"] = disk_rows
            # 多硬盘时每块盘单独换行展示（单块盘保持一行）
            headline = "\n".join(disk_head_parts) if len(disk_head_parts) > 1 else (disk_head_parts[0] if disk_head_parts else "")
            summary.append(("SSD", "硬盘", headline))

        # 兜底：WMI 完全不可用，至少给系统名
        if not sections:
            import platform, sys as _sys
            sections["系统"] = [
                ("操作系统", f"{platform.system()} {platform.release()}"),
                ("架构", platform.machine()),
                ("Python", _sys.version.split()[0]),
            ]
            summary.append(("OS", "系统", f"{platform.system()} {platform.release()}"))
    finally:
        if wmi is not None:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    ms = int((time.perf_counter() - t0) * 1000)
    return sections, summary, ms


# ── 后台采集线程 ──────────────────────────────────────────────────
class _Collector(QThread):
    done = Signal(dict, list, int)  # (sections, summary, 耗时ms)

    def run(self) -> None:
        sections, summary, ms = collect_hardware()
        self.done.emit(sections, summary, ms)


# ── 硬件信息缓存 ──────────────────────────────────────────────────
# WMI 首调要 1 秒多，每次开弹窗都重新检测太慢。首次检测完把结果写进插件自己的
# 持久化配置（%LocalAppData%\LyLaunch\plugins_hwinfo.json，由 PluginAPI 管理），
# 之后关弹窗再开、甚至重启程序都直接读缓存秒开；换硬件时点「刷新」重新检测并覆写。
_CACHE_KEY = "hardware_cache"
_CACHE_SCHEMA = 1


def _cache_load(api):
    """读缓存 → (sections, summary, saved_at)；无缓存 / 损坏 / 旧版本一律返回 None。"""
    try:
        d = api.settings_get(_CACHE_KEY, None)
        if not isinstance(d, dict) or d.get("schema") != _CACHE_SCHEMA:
            return None
        raw_sections, raw_summary = d.get("sections"), d.get("summary")
        if not isinstance(raw_sections, dict) or not isinstance(raw_summary, list):
            return None
        # JSON 会把 tuple 变 list，统一规范化回 tuple，顺便挡掉脏数据
        sections = {}
        for k, rows in raw_sections.items():
            if isinstance(rows, list):
                sections[str(k)] = [
                    (str(r[0]), str(r[1])) for r in rows
                    if isinstance(r, (list, tuple)) and len(r) >= 2
                ]
        summary = [
            (str(x[0]), str(x[1]), str(x[2])) for x in raw_summary
            if isinstance(x, (list, tuple)) and len(x) >= 3
        ]
        if not summary:
            return None
        return sections, summary, str(d.get("saved_at", ""))
    except Exception:
        return None


def _cache_save(api, sections: dict, summary: list, ms: int) -> str:
    """写缓存，返回可读时间戳；写失败返回空串（不影响界面，下次会重新检测）。"""
    from datetime import datetime
    try:
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        api.settings_set(_CACHE_KEY, {
            "schema": _CACHE_SCHEMA,
            "saved_at": saved_at,
            "sections": {k: [list(r) for r in rows] for k, rows in sections.items()},
            "summary": [list(x) for x in summary],
            "ms": int(ms),
        })
        return saved_at
    except Exception:
        return ""


# ── 紧凑概览行（横板：无卡片底，左线性硬件图标 + 右规格）────────────
# 概览 tag → 图标键（B 档）。右侧已有中文类别名，OS/CPU/GPU 这类缩写原本是冗余信息，
# 换成同主色的线性图标更干净；图标与插件瓦片共用 plugin_icons.py 一套视觉语言。
_TAG_ICON = {
    "OS": "os", "CPU": "cpu", "MB": "motherboard", "RAM": "ram",
    "GPU": "gpu", "MON": "monitor", "SSD": "disk",
}


def _icon_key_for_tag(tag: str) -> str:
    """tag → 硬件图标键；多显卡 GPU1/GPU2 归到 gpu，未知项用 info。"""
    t = (tag or "").upper()
    if t in _TAG_ICON:
        return _TAG_ICON[t]
    if t.startswith("GPU"):
        return "gpu"
    return "info"


def _hardware_pixmap(key: str, size: int = 22):
    """渲染硬件线性图标：按屏幕 DPR 给足物理像素，避免高 DPI 下发虚。"""
    dpr = 1.0
    try:
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            dpr = float(scr.devicePixelRatio()) or 1.0
    except Exception:
        dpr = 1.0
    return render_plugin_svg(generic_icon_svg(key), size, dpr)


def _make_compact_row(tag: str, category: str, headline: str) -> QWidget:
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(10)

    icon = QLabel()
    icon.setFixedWidth(34)
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setPixmap(_hardware_pixmap(_icon_key_for_tag(tag), 22))
    icon.setToolTip(category)
    hl.addWidget(icon)

    text = QVBoxLayout()
    text.setContentsMargins(0, 0, 0, 0)
    text.setSpacing(2)
    cat = QLabel(category)
    cat.setStyleSheet(f"color:{theme.TEXT_SECONDARY}; font-size:11px;")
    val = QLabel(headline)
    val.setStyleSheet(f"color:{theme.TEXT_PRIMARY}; font-size:13px; font-weight:500;")
    val.setWordWrap(True)
    text.addWidget(cat)
    text.addWidget(val)
    hl.addLayout(text, 1)
    return row


def _compact_item_builder(item) -> QWidget:
    tag, category, headline = item
    return _make_compact_row(tag, category, headline)


# ── 详细段（横板：无卡片底，段标题 + 键值行 + 细分隔线）─────────────
def _make_detail_section(title: str, rows: list[tuple[str, str]]) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 10)
    v.setSpacing(5)

    t = QLabel(title)
    t.setStyleSheet(f"color:{theme.TEXT_PRIMARY}; font-size:13px; font-weight:700;")
    v.addWidget(t)

    grid = QGridLayout()
    grid.setVerticalSpacing(3)
    grid.setHorizontalSpacing(10)
    for i, (k, val) in enumerate(rows):
        kl = QLabel(str(k))
        kl.setStyleSheet(f"color:{theme.TEXT_SECONDARY}; font-size:12px;")
        vl = QLabel(str(val))
        vl.setStyleSheet(f"color:{theme.TEXT_PRIMARY}; font-size:13px;")
        vl.setWordWrap(True)
        grid.addWidget(kl, i, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(vl, i, 1, Qt.AlignmentFlag.AlignTop)
    grid.setColumnMinimumWidth(0, 72)
    v.addLayout(grid)

    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFrameShadow(QFrame.Shadow.Plain)
    sep.setStyleSheet(f"color:{theme.BORDER};")
    v.addWidget(sep)
    return w


def _grid_widget(items, builder, cols: int) -> QWidget:
    """把 items 排成 cols 列的网格容器（用于横板布局）。"""
    grid = QGridLayout()
    grid.setVerticalSpacing(16)
    grid.setHorizontalSpacing(24)
    grid.setContentsMargins(0, 0, 0, 0)
    for c in range(cols):
        grid.setColumnStretch(c, 1)
    for i, item in enumerate(items):
        r, c = divmod(i, cols)
        grid.addWidget(builder(item), r, c)
    w = QWidget()
    w.setLayout(grid)
    return w


# ── 紧凑概览主弹窗（横板）─────────────────────────────────────────
class HwDialog(QDialog):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self._sections: dict[str, list[tuple[str, str]]] = {}
        self._summary: list[tuple[str, str, str]] = []
        self._worker: _Collector | None = None

        self.setWindowTitle("硬件信息")
        # 装了 44px 自绘导航栏后内容变矮（截图里「显示器」被截），默认高度加大
        self.resize(820, 580)
        self.setMinimumSize(640, 440)
        self.setStyleSheet(theme.DIALOG_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(14)
        # 外壳必须在 root 布局建好之后装（否则拿不到 layout，会走保守回退）
        from liytools.plugins.window_chrome import install_chrome
        install_chrome(self, "硬件信息", show_minmax=True, resizable=True)

        # 头部：标题 + 右上「查看详情」
        head = QHBoxLayout()
        head.setSpacing(12)
        htext = QVBoxLayout()
        htext.setSpacing(2)
        title = QLabel("硬件信息")
        title.setProperty("role", "title")
        sub = QLabel("本机硬件概览")
        sub.setProperty("role", "subtitle")
        htext.addWidget(title)
        htext.addWidget(sub)
        head.addLayout(htext, 1)
        self.detail_btn = QPushButton("查看详情")
        self.detail_btn.setProperty("role", "primary")
        self.detail_btn.clicked.connect(self._open_detail)
        head.addWidget(self.detail_btn)
        root.addLayout(head)

        # 两列网格的紧凑概览
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.viewport().setStyleSheet("background:transparent;")
        root.addWidget(self.scroll, 1)

        # 底部：状态 + 刷新 / 复制
        bar = QWidget()
        bar.setProperty("role", "statusBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 10, 0, 0)
        self.status = QLabel("准备中…")
        self.status.setProperty("role", "status")
        bl.addWidget(self.status, 1)
        self.copy_btn = QPushButton("复制全部")
        self.copy_btn.setProperty("role", "ghost")
        self.copy_btn.clicked.connect(self._copy_all)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setProperty("role", "soft")
        self.refresh_btn.setToolTip("重新检测硬件（更换硬件后点这里）")
        self.refresh_btn.clicked.connect(self._run)
        bl.addWidget(self.copy_btn)
        bl.addWidget(self.refresh_btn)
        root.addWidget(bar)

        # 优先读缓存秒开；只有首次（或缓存损坏/旧版本）才真正跑 WMI 检测
        cached = _cache_load(self._api())
        if cached is not None:
            sections, summary, saved_at = cached
            self._apply(sections, summary)
            self.status.setText(f"缓存 · {saved_at} · 刷新可重新检测")
            self.refresh_btn.setEnabled(True)
            self.copy_btn.setEnabled(bool(summary))
        else:
            self._run()

    def _api(self):
        """安全取插件 API（独立运行 / 测试时 ctx 可能为 None）。"""
        try:
            return self.ctx.api
        except Exception:
            return None

    def _apply(self, sections, summary) -> None:
        """把一份结果（缓存或新采集）铺到界面上。"""
        self._sections = sections
        self._summary = summary
        if not summary:
            body = QLabel("未能读取到硬件信息。")
        else:
            body = _grid_widget(summary, _compact_item_builder, 1)
        self._set_body(body)

    def _run(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.status.setText("正在检测硬件信息…")
        # 不清空正文：刷新时先保留上一次结果，检测完整体替换，避免闪一下白屏
        self._worker = _Collector()
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _set_body(self, widget) -> None:
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        if widget is not None:
            self.scroll.setWidget(widget)

    def _on_done(self, sections: dict, summary: list, ms: int) -> None:
        self._worker = None
        self._apply(sections, summary)
        # 结果够完整才落缓存：WMI 全挂时只有一条 OS 兜底，不该把异常结果固化下来
        if len(summary) >= 3:
            saved = _cache_save(self._api(), sections, summary, ms)
            self.status.setText(f"已检测 · 用时 {ms} ms" + (" · 已缓存" if saved else ""))
        else:
            self.status.setText(f"已检测 · 用时 {ms} ms · 结果不完整未缓存")
        self.refresh_btn.setEnabled(True)
        self.copy_btn.setEnabled(bool(summary))

    def _open_detail(self) -> None:
        global _DETAIL_DLG
        if not self._sections:
            return
        if _DETAIL_DLG is not None:
            try:
                _DETAIL_DLG.raise_()
                _DETAIL_DLG.activateWindow()
                return
            except RuntimeError:
                _DETAIL_DLG = None
        dlg = DetailDialog(self.ctx, self._sections)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: globals().__setitem__("_DETAIL_DLG", None))
        _DETAIL_DLG = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _copy_all(self) -> None:
        if not self._sections:
            return
        lines = []
        for title, rows in self._sections.items():
            lines.append(f"【{title}】")
            for k, v in rows:
                lines.append(f"  {k}：{v}")
            lines.append("")
        self.ctx.api.clipboard_write("\n".join(lines))
        self.status.setText("已复制全部硬件信息到剪贴板")

    def closeEvent(self, e) -> None:
        if self._worker is not None:
            try:
                self._worker.done.disconnect(self._on_done)
            except Exception:
                pass
        super().closeEvent(e)


# ── 详细视图（横板：各部件分段，无卡片底）──────────────────────────
class DetailDialog(QDialog):
    def __init__(self, ctx, sections: dict) -> None:
        super().__init__()
        self.ctx = ctx
        self._sections = sections
        self.setWindowTitle("硬件详情")
        self.resize(860, 640)
        self.setMinimumSize(680, 480)
        self.setStyleSheet(theme.DIALOG_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(14)
        # 外壳必须在 root 布局建好之后装（否则拿不到 layout，会走保守回退）
        from liytools.plugins.window_chrome import install_chrome
        install_chrome(self, "硬件详情", show_minmax=True, resizable=True)
        title = QLabel("硬件详情")
        title.setProperty("role", "title")
        sub = QLabel("逐项硬件规格")
        sub.setProperty("role", "subtitle")
        root.addWidget(title)
        root.addWidget(sub)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.viewport().setStyleSheet("background:transparent;")
        root.addWidget(self.scroll, 1)

        bar = QWidget()
        bar.setProperty("role", "statusBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 10, 0, 0)
        self.status = QLabel("")
        self.status.setProperty("role", "status")
        bl.addWidget(self.status, 1)
        copy_btn = QPushButton("复制全部")
        copy_btn.setProperty("role", "ghost")
        copy_btn.clicked.connect(self._copy_all)
        bl.addWidget(copy_btn)
        root.addWidget(bar)

        items = list(sections.items())
        self.scroll.setWidget(_grid_widget(items, _detail_item_builder, 1))

    def _copy_all(self) -> None:
        if not self._sections:
            return
        lines = []
        for title, rows in self._sections.items():
            lines.append(f"【{title}】")
            for k, v in rows:
                lines.append(f"  {k}：{v}")
            lines.append("")
        self.ctx.api.clipboard_write("\n".join(lines))
        self.status.setText("已复制全部硬件信息到剪贴板")


def _detail_item_builder(item) -> QWidget:
    title, rows = item
    return _make_detail_section(title, rows)


# ── 非模态打开（第十章头号红线）──────────────────────────────────
def _open_nonmodal(ctx) -> None:
    global _DLG
    if _DLG is not None:
        try:
            _DLG.raise_()
            _DLG.activateWindow()
            return
        except RuntimeError:
            _DLG = None
    dlg = HwDialog(ctx)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dlg.destroyed.connect(lambda *_: globals().__setitem__("_DLG", None))
    _DLG = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


class Plugin(PluginBase):
    def on_execute(self, query: str = "") -> None:
        _open_nonmodal(self.ctx)
