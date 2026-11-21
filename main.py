"""LyLaunch 入口。

职责：
- 单实例保护（命名 Mutex + 显示信号事件）
- 创建 QApplication、主窗口、系统托盘
- 串起热键 / 剪贴板 / 图标后台加载
- 主循环运行

运行方式：在项目根目录执行  python main.py
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


class _SafeQApplication(QApplication):
    """事件 / 槽回调里逃逸的异常，Qt 默认会直接 abort 整个进程
    （sys.excepthook 抓不到那一类，见 _install_excepthook 的注释）。

    这里在分发层统一兜底：写 crash.log + 打印 stderr，并返回 True（当作已处理），
    避免「一个插件的小异常把整个启动器带崩」。异常被吞掉后务必留痕，方便事后定位。
    """

    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            import datetime
            import traceback
            try:
                from lylaunch import paths
                paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(paths.DATA_DIR / "crash.log", "a", encoding="utf-8") as f:
                    f.write("\n==== event/slot exception ")
                    f.write(datetime.datetime.now().isoformat(timespec="seconds"))
                    f.write(" ====\n")
                    f.write("".join(traceback.format_exc()))
                    f.write("\n")
            except Exception:
                pass
            traceback.print_exc()
            return True

from lylaunch import single_instance, tray, paths
from lylaunch.icons import get_icon
from lylaunch.ui.main_window import MainWindow


def _dbg_print(*args, **kwargs):
    """调试输出已停用；保留调用以维持语句结构。"""
    pass



def _install_excepthook() -> None:
    """兜底未捕获异常：写进 %LocalAppData%/LyLaunch/crash.log 并原样打印到 stderr。

    作用：双击(pythonw)无控制台运行时，异常不会"静默消失"——至少留痕可查。
    注意：Qt 在事件回调里逃逸的异常会先 abort 进程，本钩子捕获不到那一类；
    插件执行异常由 manager.execute 的 BaseException 兜底捕获（见 lylaunch/plugins/manager.py）。
    """
    import traceback

    _orig = sys.excepthook

    def _hook(et, ev, tb):
        try:
            paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(paths.DATA_DIR / "crash.log", "a", encoding="utf-8") as f:
                f.write("\n==== crash ", flush=True)
                import datetime
                f.write(datetime.datetime.now().isoformat(timespec="seconds"))
                f.write(" ====\n")
                f.write("".join(traceback.format_exception(et, ev, tb)))
                f.write("\n")
        except Exception:
            pass
        _orig(et, ev, tb)

    sys.excepthook = _hook


_QT_MSG_HANDLER = None  # 模块级强引用：PySide6 下 handler 若被 GC，消息处理会静默失效


def _install_qt_noise_filter() -> None:
    """丢掉 Qt 在 Windows 上的已知字体噪音。

    Qt 解析默认字体链时会去尝试 MS Sans Serif / Fixedsys 这些早已废弃的点阵
    字体（名字还能解析，但 DirectWrite 建不出字体面），于是反复打印
    "CreateFontFaceFromHDC() failed"。实际渲染早已 fallback 到系统字体，
    界面完全正常 —— 纯噪音。只丢这一条，别的 qDebug / qWarning 照常输出。
    """
    from PySide6.QtCore import qInstallMessageHandler

    def _handler(_mode, _ctx, msg: str) -> None:
        if "CreateFontFaceFromHDC" in msg:
            return
        # 走字节流并强制 utf-8：直接 write 字符串在非 utf-8 控制台会抛编码异常
        try:
            buf = getattr(sys.stderr, "buffer", None)
            if buf is not None:
                buf.write((msg + "\n").encode("utf-8", "replace"))
                buf.flush()
            else:
                sys.stderr.write(msg + "\n")
        except Exception:
            pass

    global _QT_MSG_HANDLER
    _QT_MSG_HANDLER = _handler  # 保持强引用，防止被回收后静默失效
    qInstallMessageHandler(_handler)


_TOOLTIP_BLOCKER = None  # 模块级强引用：QObject 被 GC 后事件过滤器会静默失效


def _install_tooltip_blocker(app: QApplication) -> None:
    """全程序关掉 hover 解释气泡（0.39.1 设计要求）。

    用户不想要「鼠标停在按钮上一两秒，弹出一串功能说明」的深色气泡。
    这类提示散落在 6 个插件 + 主界面/设置页，共几十处，而且不少嵌在状态
    同步逻辑里（if/elif 分支里动态改文案），逐个删既容易漏、又会动到业务
    流程 —— 在这里按事件类型统一拦掉：所有窗口（含以后新增的插件）一次性
    生效，要恢复只要删掉这一个函数。

    ⚠️ 只拦 QEvent.ToolTip（hover 解释）。系统托盘图标名称走 Windows shell，
    不经过这个事件，会正常保留；插件里 QToolTip.showText() 主动弹的操作反馈
    （例如「没有可撤销的操作」）也不受影响。
    """
    from PySide6.QtCore import QEvent, QObject

    class _Blocker(QObject):
        def eventFilter(self, obj, event):  # noqa: N802
            if event.type() == QEvent.Type.ToolTip:
                return True
            return False

    global _TOOLTIP_BLOCKER
    _TOOLTIP_BLOCKER = _Blocker()
    _TOOLTIP_BLOCKER.setParent(app)   # 生命周期跟随 app，不悬空
    app.installEventFilter(_TOOLTIP_BLOCKER)


def main() -> int:
    _install_excepthook()
    _install_qt_noise_filter()

    # ① 单实例：已有实例在跑，通知它把窗口唤出来，自己退出
    if not single_instance.is_first_instance():
        _dbg_print("[LyLaunch] 检测到已有实例在运行，已发送唤出信号，本进程退出。")
        single_instance.signal_existing_instance()
        return 0

    _dbg_print("[LyLaunch] 首次启动，正在创建窗口…", flush=True)
    app = _SafeQApplication(sys.argv)
    app.setApplicationName("LyLaunch")
    _install_tooltip_blocker(app)   # 全程序禁用 hover 解释气泡

    # 全局推理引擎（llama.cpp Router）：懒启动（此处只构造，不 spawn）；
    # 退出时统一收口，保证 router + 所有模型 child 被清理（含右键退出/异常退出）。
    from lylaunch.engine import EngineManager
    _engine_mgr = EngineManager.instance()
    app.aboutToQuit.connect(_engine_mgr.shutdown)

    # 字体兜底：Qt 在 Windows 上的默认字体链会落到 MS Sans Serif / Fixedsys
    # 这类旧点阵字体 —— 它们名字能解析、但 DirectWrite 画不出来，于是反复打印
    # "CreateFontFaceFromHDC() failed"（无害但很吵）。
    # ① 字体替换把 MS Shell Dlg 系指到雅黑（对 MS Sans Serif 本身无效：那个
    #    族名在系统里能解析，替换表不会触发，实测确认）；
    # ② 真正管用的是下面 setFont —— 所有控件继承它，就没人再去请求旧族名了。
    #    插件自己的 QSS 也补了 font-family，双保险。
    from PySide6.QtGui import QFont
    for _old, _new in (("MS Sans Serif", "Microsoft YaHei UI"),
                       ("MS Shell Dlg", "Microsoft YaHei UI"),
                       ("MS Shell Dlg 2", "Microsoft YaHei UI"),
                       ("Fixedsys", "Consolas")):
        QFont.insertSubstitution(_old, _new)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    # 留个可核对的依据：启动时打印一次，便于确认这份字体设置是否真的跑到
    _dbg_print(f"[LyLaunch] 全局字体：{app.font().family()} {app.font().pointSize()}pt",
          flush=True)
    # 托盘驻留程序：主窗口只是隐藏(hide)，不应因"最后一个窗口关闭"而退出进程。
    app.setQuitOnLastWindowClosed(False)

    # 取本程序图标：优先用 assets/tray.ico（开发期直接生效），
    # 打包成 exe 后再回退 exe 内嵌图标
    ico = paths.resource_path("assets", "tray.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    else:
        try:
            exe = QApplication.applicationFilePath()
            icon_pix = get_icon(exe) if exe and os.path.exists(exe) else None
            if icon_pix is not None:
                app.setWindowIcon(QIcon(icon_pix))
        except Exception:
            pass

    _dbg_print("[LyLaunch] 正在构造 MainWindow…", flush=True)
    try:
        window = MainWindow()
    except Exception as e:  # 暴露构造期静默崩溃
        import traceback
        traceback.print_exc()
        return 1
    _dbg_print("[LyLaunch] MainWindow 构造完成。", flush=True)
    window.show_requested.connect(window.show_window)

    # ② 系统托盘
    def show_cb():
        window.show_window()

    def exit_cb():
        # 走 quit_app 而不是 close()：closeEvent 在「最小化到托盘」模式下只隐藏
        window.quit_app()

    tray_icon = tray.setup_tray(show_cb, exit_cb)
    window._tray = tray_icon      # 设置页「关闭到托盘」与首提示要用到它
    _dbg_print("[LyLaunch] 托盘已设置。", flush=True)

    _dbg_print("[LyLaunch] 调用 show_window()…", flush=True)
    window.show_window()  # 首次启动直接显示浮窗（带居中 + 激活 + 隐藏保护）
    _dbg_print("[LyLaunch] show_window() 返回。", flush=True)

    _dbg_print(f"[LyLaunch] 窗口已显示，进入主循环。按 "
          f"{getattr(window, '_hotkey_display', 'Alt+Space')} 唤出/隐藏；托盘可右键退出。")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
