"""真实图标提取（对应 C# 的 SHDefExtractIcon + BitmapToImageSource）。

方案：ctypes 调 shell32.SHDefExtractIcon 直接拿原生 HICON（真原生，最清晰），
再转成 QImage（线程安全，可在后台工作线程跑）。主线程把 QImage 转 QPixmap 显示。
取不到时退回 QFileIconProvider（系统关联图标）兜底。

所有调用包在 try/except；图标失败绝不让程序崩——最多显示字母兜底。
"""

from __future__ import annotations

import ctypes
import os
import queue
from ctypes import POINTER, byref, c_int, c_uint, c_void_p, cast, create_string_buffer, addressof
from ctypes.wintypes import BOOL, DWORD, HANDLE, HBITMAP, HICON

from PySide6.QtCore import QThread, Signal, QFileInfo
from PySide6.QtGui import QImage, QPixmap, QIcon
from PySide6.QtWidgets import QFileIconProvider

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

# ---- 结构体 ----
class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD),
        ("hbmMask", HBITMAP), ("hbmColor", HBITMAP),
    ]

class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_short),
        ("bmBitsPixel", ctypes.c_short), ("bmBits", c_void_p),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", c_uint), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
        ("biCompression", c_uint), ("biSizeImage", c_uint),
        ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", c_uint), ("biClrImportant", c_uint),
    ]

class SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", HICON),
        ("iIcon", c_int),
        ("dwAttributes", DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


# ---- 函数签名 ----
shell32.SHDefExtractIconW.argtypes = [ctypes.c_wchar_p, c_int, c_uint, POINTER(HICON), POINTER(HICON), c_uint]
shell32.SHDefExtractIconW.restype = c_int
shell32.SHGetFileInfoW.argtypes = [ctypes.c_wchar_p, DWORD, ctypes.POINTER(SHFILEINFO), c_uint, c_uint]
shell32.SHGetFileInfoW.restype = c_void_p
# PrivateExtractIconsW：按指定尺寸精确提取图标（资源里有 256 就给 256，最清晰）。
# 注意：这个函数导出在 user32.dll，不是 shell32。
user32.PrivateExtractIconsW.argtypes = [ctypes.c_wchar_p, c_int, c_int, c_int, POINTER(HICON), POINTER(c_uint), c_uint, c_uint]
user32.PrivateExtractIconsW.restype = c_uint
user32.GetIconInfo.argtypes = [HICON, POINTER(ICONINFO)]
user32.GetIconInfo.restype = BOOL
user32.DestroyIcon.argtypes = [HICON]
user32.DestroyIcon.restype = BOOL
gdi32.GetObjectW.argtypes = [HANDLE, c_int, c_void_p]
gdi32.GetObjectW.restype = c_int
gdi32.GetDIBits.argtypes = [HANDLE, HBITMAP, c_uint, c_uint, c_void_p, c_void_p, c_uint]
gdi32.GetDIBits.restype = c_int
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteObject.restype = BOOL

_pixmap_cache: dict[str, QPixmap] = {}

SHGFI_ICON = 0x100
SHGFI_LARGEICON = 0x0


def _hicon_to_image(hicon: HICON) -> QImage | None:
    """HICON → QImage（线程安全，可在后台线程跑）。手动画 BITMAP 转换，覆盖彩色/掩码图标。"""
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, byref(ii)):
        return None
    try:
        bmp = ii.hbmColor if ii.hbmColor else ii.hbmMask
        if not bmp:
            return None
        bm = BITMAP()
        if gdi32.GetObjectW(bmp, ctypes.sizeof(bm), byref(bm)) == 0:
            return None
        width, height = bm.bmWidth, abs(bm.bmHeight)
        if width <= 0 or height <= 0:
            return None
        # 纯掩码位图高度为实际两倍（含 AND 掩码），取一半
        if not ii.hbmColor and ii.hbmMask:
            height //= 2
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = width
        bi.biHeight = -height
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf_size = width * height * 4
        buf = create_string_buffer(buf_size)
        dc = user32.GetDC(0)
        ret = gdi32.GetDIBits(dc, bmp, 0, height, buf, byref(bi), 0)
        user32.ReleaseDC(0, dc)
        if ret == 0:
            return None
        # PySide6 的 QImage.scanLine() 返回 memoryview（PyQt6 返回可 int() 的指针），
        # 直接 QImage(bytes, w, h, stride, fmt).copy() 更稳、跨绑定通用。
        buf_ptr = cast(buf, c_void_p)
        raw = ctypes.string_at(buf_ptr.value, width * height * 4)
        return QImage(raw, width, height, width * 4,
                      QImage.Format.Format_ARGB32).copy()
    except Exception:
        return None
    finally:
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)


def _shell_file_handle(path: str) -> HICON | None:
    """用 SHGetFileInfo 取系统“显示图标”（对 .lnk 会自动解析到目标 exe 图标，
    覆盖比 SHDefExtractIcon 更广，作为原生提取失败时的兜底）。"""
    psfi = SHFILEINFO()
    ret = shell32.SHGetFileInfoW(
        path, 0, byref(psfi), ctypes.sizeof(SHFILEINFO), SHGFI_ICON | SHGFI_LARGEICON
    )
    if ret and psfi.hIcon:
        return psfi.hIcon
    return None


def _native_handle(path: str, size: int) -> HICON | None:
    ph_large = HICON()
    ph_small = HICON()
    if shell32.SHDefExtractIconW(path, 0, 0, byref(ph_large), byref(ph_small), size) == 0:
        h = ph_large.value or ph_small.value
        if h:
            return h
    return None


def _private_extract(path: str, size: int, index: int = 0) -> HICON | None:
    """PrivateExtractIconsW（user32）：按指定尺寸从图标资源里精确提取（优先用它拿高清大图）。"""
    icons = (HICON * 1)()
    n = user32.PrivateExtractIconsW(path, index, size, size, icons, None, 1, 0)
    if n and icons[0]:
        return icons[0]
    return None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, s: str) -> None:
        import re
        m = re.match(
            r"\{?([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-"
            r"([0-9a-fA-F]{2})([0-9a-fA-F]{2})-([0-9a-fA-F]{2})([0-9a-fA-F]{2})"
            r"([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", s)
        if not m:
            raise ValueError(s)
        d = [int(m.group(i), 16) for i in range(1, 12)]
        self.Data1 = d[0]
        self.Data2 = d[1]
        self.Data3 = d[2]
        for i in range(8):
            self.Data4[i] = d[3 + i]


_CLSID_ShellLink = _GUID("{00021401-0000-0000-C000-000000000046}")
_IID_IShellLinkW = _GUID("{000214F9-0000-0000-C000-000000000046}")
_IID_IPersistFile = _GUID("{0000010B-0000-0000-C000-000000000046}")
_CLSCTX_INPROC_SERVER = 1


def _vfunc(iface, idx: int) -> int:
    """从 COM 接口指针取出第 idx 个虚函数地址（整数）。"""
    vtbl_ptr = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]
    arr = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p * 100)).contents
    return int(arr[idx])


def _resolve_lnk_ex(path: str) -> tuple[str | None, str, int]:
    """解析 .lnk：返回 ``(目标路径, 图标文件, 图标索引)``。

    图标文件（IconLocation）不能丢 —— 像 Ollama 这类 Electron 应用，
    exe 里提不出图标资源，真正的图标放在同目录的 app.ico 上，
    快捷方式只在 IconLocation 字段里记着它。只读 target 就会取不到图标。
    """
    ole32 = ctypes.windll.ole32
    try:
        ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize.restype = ctypes.c_long
    except Exception:
        pass
    try:
        ole32.CoInitialize(None)
    except Exception:
        pass
    psl = ctypes.c_void_p()
    ppf = ctypes.c_void_p()
    target: str | None = None
    icon_path = ""
    icon_index = 0
    try:
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        if ole32.CoCreateInstance(
                ctypes.byref(_CLSID_ShellLink), None, _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_IShellLinkW), ctypes.byref(psl)) != 0 or not psl:
            return None, "", 0

        QI = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        if QI(_vfunc(psl, 0))(psl, ctypes.byref(_IID_IPersistFile), ctypes.byref(ppf)) != 0 or not ppf:
            return None, "", 0

        Load = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong)
        if Load(_vfunc(ppf, 5))(ppf, path, 0) != 0:
            return None, "", 0

        GetPath = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p,
            ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
        buf = ctypes.create_unicode_buffer(1024)
        if GetPath(_vfunc(psl, 3))(psl, buf, 1024, None, 0) == 0 and buf.value:
            target = buf.value

        # GetIconLocation：vtable 里 IUnknown(0-2) 之后 GetPath=3，逐个 +1 → 16
        try:
            GetIcon = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p,
                ctypes.c_int, ctypes.POINTER(ctypes.c_int))
            ibuf = ctypes.create_unicode_buffer(1024)
            index = ctypes.c_int(0)
            if GetIcon(_vfunc(psl, 16))(psl, ibuf, 1024, ctypes.byref(index)) == 0:
                icon_path = ibuf.value or ""
                icon_index = int(index.value)
        except Exception:
            pass
    except Exception:
        return None, "", 0
    finally:
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        for obj in (ppf, psl):
            if obj:
                try:
                    Release(_vfunc(obj, 2))(obj)
                except Exception:
                    pass
    return target, icon_path, icon_index


def _resolve_lnk(path: str) -> str | None:
    """用 IShellLink 解析 .lnk 的真实目标路径（exe/文档等）。

    取图标时改用目标路径提取，就能绕开 Windows 给 .lnk 自动加的「快捷方式箭头」叠加层。
    解析失败或指向非本地文件时返回 None，调用方回退到原 .lnk 路径。
    """
    target, _icon_path, _index = _resolve_lnk_ex(path)
    return target


def _read_url_icon(path: str) -> tuple[str, int]:
    """读 .url 快捷方式的 IconFile / IconIndex。

    Steam 游戏的桌面快捷方式是 .url（steam:// 协议），真图标在
    IconFile=...steam/games/xxx.ico 里 —— 不读这个就只能拿到 IE 地球。
    """
    icon_file = ""
    icon_index = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                text = line.strip()
                low = text.casefold()
                if low.startswith("iconfile="):
                    icon_file = text[len("iconfile="):].strip()
                elif low.startswith("iconindex="):
                    try:
                        icon_index = int(text[len("iconindex="):].strip())
                    except ValueError:
                        icon_index = 0
                if icon_file and low.startswith("iconindex="):
                    break          # 两个字段都拿到了
    except OSError:
        pass
    return icon_file, icon_index


def extract_icon_image(path: str, size: int = 48) -> QImage | None:
    """线程安全：返回 QImage（后台线程可用）。

    取图标优先级：PrivateExtractIconsW 256 原生档 → SHDefExtractIcon 256 → 48 → SHGetFileInfo 系统显示图标。
    优先取高分辨率（256）再交给瓦片缩小到显示尺寸，图标更清晰、不糊。
    .lnk 快捷方式先解析到真实目标再取图标，从而去掉左下角的「快捷方式箭头」。

    拿不到返回 None（调用方会保留字母占位兜底）。
    """
    if not path:
        return None
    real = path
    icon_path = ""
    icon_index = 0
    low = path.lower()
    if low.endswith(".lnk"):
        try:
            target, ico, idx = _resolve_lnk_ex(path)
            if target and os.path.exists(target):
                real = target
            if ico and os.path.exists(ico):
                icon_path = ico
                icon_index = idx
        except Exception:
            pass
    elif low.endswith(".url"):
        try:
            ico, idx = _read_url_icon(path)
            if ico and os.path.exists(ico):
                icon_path = ico
                icon_index = idx
        except Exception:
            pass
    try:
        handle = None
        # ① 快捷方式里记录的图标文件优先：Electron 应用（如 Ollama）的 exe 提不出
        #    图标资源，真正的图标只在同目录的 .ico 上，只有 IconLocation 记着它。
        if icon_path:
            handle = (_private_extract(icon_path, 256, icon_index)
                      or _native_handle(icon_path, 256))
        # ② 目标 exe / 文件的图标资源
        if not handle:
            handle = (_private_extract(real, 256) or _native_handle(real, 256)
                      or _native_handle(real, 48) or _shell_file_handle(real))
        if handle:
            try:
                return _hicon_to_image(handle)
            finally:
                user32.DestroyIcon(handle)
    except Exception:
        pass
    return None


def get_icon(path: str, size: int = 48) -> QPixmap | None:
    """主线程取图标（返回 QPixmap）。优先原生，退回 QFileIconProvider。"""
    if not path:
        return None
    if path in _pixmap_cache:
        return _pixmap_cache[path]
    result: QPixmap | None = None
    try:
        img = extract_icon_image(path, size)
        if img is not None:
            result = QPixmap.fromImage(img)
        else:
            icon = QFileIconProvider().icon(QFileInfo(path))
            if not icon.isNull():
                result = icon.pixmap(size, size)
    except Exception:
        result = None
    _pixmap_cache[path] = result  # type: ignore[assignment]
    return result


class IconLoader(QThread):
    """后台图标加载器（对应 C# 的 IconWorker 单线程串行取图标）。

    主线程把 (item, 图标来源路径) 入队；本线程提取出 QImage 后发回主线程转 QPixmap。
    QImage 可跨线程传递，QPixmap 必须在主线程创建。
    """
    loaded = Signal(object, QImage)
    _instance: "IconLoader | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._q: "queue.Queue[tuple]" = queue.Queue()

    @classmethod
    def instance(cls) -> "IconLoader":
        if cls._instance is None:
            cls._instance = IconLoader()
            cls._instance.start()
        return cls._instance

    def enqueue(self, item, path: str) -> None:
        if path:
            self._q.put((item, path))

    def run(self) -> None:
        while True:
            try:
                item, path = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                img = extract_icon_image(path, 256)
                if img is not None:
                    self.loaded.emit(item, img)
            except Exception:
                pass
