"""模型文件下载（ModelScope 镜像 + sha256 校验）。

0.39.1 决策：llama-server 运行时随安装包内置，不在这里下载；
本模块只负责 GGUF 模型（主模型 + 可选 mmproj）。已存在则跳过。

0.39.5：支持**暂停 / 恢复**与**断点续传**。
暂停不是「卡住不读」（那样服务端会把空闲连接掐掉、恢复时进度全丢），
而是主动断开连接、把已下载字节留在 ``.part`` 里，恢复时用 HTTP Range
从断点续传。ModelScope 的 CDN 实测支持 Range（206 Partial Content）。

取消和暂停是两件事：**暂停**保留 ``.part`` 等续传；**取消**表示这次不下了，
``_download`` 删掉当前 ``.part``，调用方再 ``cleanup_model_dir()`` 清掉该模型
目录里已下好的文件与空目录（设置页和本地 AI 下载弹窗都这么做）。
"""
from __future__ import annotations

import hashlib
import time
import urllib.request
from pathlib import Path

from . import registry


class DownloadCancelled(Exception):
    """用户主动取消下载 —— 与「网络失败」严格区分。

    0.39.1 前取消只能靠 progress 回调抛异常，但下面两处 `except Exception`
    会把它当普通失败：一次吞在进度回调里、一次被「换镜像重试」吞掉继续下，
    结果就是点了「取消下载」下载还在跑。现在用独立异常类型一路透传，
    绝不触发镜像重试。
    """


# ModelScope 两种 URL 形式都试（不同仓库对 master/revision 的兼容性不同）
_MS_URLS = (
    "https://modelscope.cn/models/{repo}/resolve/master/{name}",
    "https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={name}",
)

_CHUNK = 1024 * 256


def _hold_while_paused(paused, cancelled) -> None:
    """暂停期间原地等待；此连接已断开，所以等待期不占带宽、也不会被服务端超时。"""
    while paused is not None and paused():
        if cancelled is not None and cancelled():
            raise DownloadCancelled()
        time.sleep(0.12)


def _safe(cb, *args) -> None:
    """UI 回调自身出错不该中断下载，但取消信号必须继续上抛。"""
    try:
        cb(*args)
    except DownloadCancelled:
        raise
    except Exception:
        pass


def _download(url: str, dst: Path, progress=None, cancelled=None,
              paused=None, on_bytes=None) -> None:
    """下载单个文件，支持暂停/续传。

    - ``progress(doc, pct|None)``：百分比进度（总长未知时 pct 不回调）
    - ``cancelled() -> bool``：返回 True 即抛 DownloadCancelled，``.part`` 保留
    - ``paused() -> bool``：返回 True 则断开连接等待，恢复后用 Range 续传
    - ``on_bytes(name, got, total)``：真实字节（速度 / 剩余时间用）
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    total = 0
    try:
        while True:
            _hold_while_paused(paused, cancelled)
            offset = tmp.stat().st_size if tmp.is_file() else 0
            req = urllib.request.Request(url)
            if offset:
                req.add_header("Range", f"bytes={offset}-")   # 续传：只要剩下的
            with urllib.request.urlopen(req, timeout=60) as r:
                code = 200
                try:
                    code = int(r.getcode() or 200)
                except Exception:
                    pass
                resumed = offset > 0 and code == 206
                if offset and not resumed:
                    # 服务器不认 Range（返回 200 全量）：只能从头写，先清掉残片
                    offset = 0
                clen = int(r.headers.get("Content-Length") or 0)
                if clen:
                    total = clen + offset
                got = offset
                pausing = False
                with open(tmp, "ab" if resumed else "wb") as f:
                    while True:
                        if cancelled is not None and cancelled():
                            raise DownloadCancelled()
                        if paused is not None and paused():
                            pausing = True          # 断开连接 → 回外层等恢复
                            break
                        chunk = r.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if on_bytes is not None:
                            _safe(on_bytes, dst.name, got, total)
                        if progress is not None and total:
                            _safe(progress, f"下载 {dst.name}", int(got * 100 / total))
            if not pausing:
                break
        tmp.replace(dst)
    except DownloadCancelled:
        # 取消 = 放弃这次下载：.part 直接删掉，不留给「继续」。
        # 暂停走的是 pausing 分支（不抛异常），所以暂停仍保留 .part 可续传。
        # 这里文件句柄已经关了，删得掉；外层的「取消后清目录」再收尾已完成的文件。
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def ensure_model_files(spec, progress=None, cancelled=None,
                       paused=None, on_bytes=None) -> None:
    """确保主模型 + mmproj 就位（已有则跳过；sha256 有则强校验）。"""
    targets = [(spec.file, spec.sha256), (spec.mmproj, "")]
    for name, sha in targets:
        if not name:
            continue
        dst = registry.models_dir() / spec.id / name
        if dst.is_file():
            continue
        ok = False
        for repo in spec.repos:
            for tpl in _MS_URLS:
                url = tpl.format(repo=repo, name=name)
                try:
                    _download(url, dst, progress, cancelled, paused, on_bytes)
                    if sha and _sha256(dst) != sha:
                        dst.unlink(missing_ok=True)
                        continue
                    ok = True
                    break
                except DownloadCancelled:
                    raise          # 用户取消：别换镜像把下载又拉起来
                except Exception:
                    continue
            if ok:
                break
        if not ok:
            raise RuntimeError(f"模型 {spec.id} 的 {name} 下载失败（所有镜像都不可用）")


def cleanup_model_dir(spec) -> tuple[int, str]:
    """取消下载后的清场：删掉 engine/models/<id> 下的文件（含 .part），再删空目录。

    只动统一目录，**不碰 legacy_dirs**——那里是和别的模型共用的旧目录（比如
    Xiaomi-OCR-0 和 OvisOCR2 都指向 models/xiaomi-ocr-0/models），按目录清会把
    另一个模型的文件一起带走。取消本身只意味着「这个模型这次没下成」。

    返回 (删掉的文件数, 最后一个错误)；有错误通常是句柄还没放开，调用方等一下可重试。
    """
    d = registry.models_dir() / spec.id
    if not d.is_dir():
        return 0, ""
    removed = 0
    err = ""
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        for _ in range(6):
            try:
                p.unlink()
                removed += 1
                break
            except FileNotFoundError:
                break
            except PermissionError as exc:
                # Windows 上线程刚退出时句柄可能还没释放，等半秒再试
                err = str(exc)
                time.sleep(0.5)
            except Exception as exc:
                err = str(exc)
                break
    try:
        for sub in sorted(d.rglob("*"), reverse=True):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except Exception:
                    pass
        d.rmdir()
    except Exception as exc:
        err = err or str(exc)
    return removed, err
