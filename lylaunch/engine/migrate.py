"""把旧目录的模型硬链接进 engine/models/<id>/。

设计要点：
- **不删旧文件**：回退旧代码后旧路径仍可找到模型，不重下数 GB；
  硬链接不额外占空间（同一份数据两个名字）。
- 幂等：已存在 engine/models/<id>/<file> 就跳过。
- 跨卷 / 权限失败时退化为复制（保证可用，牺牲一点磁盘）。
"""
from __future__ import annotations

import os
import shutil

from . import registry


def adopt_legacy(progress=None) -> int:
    """返回新建立的链接数（0 = 无需迁移或已迁移过）。"""
    made = 0
    for spec in registry.MODELS:
        dst_dir = registry.models_dir() / spec.id
        for name in (spec.file, spec.mmproj):
            if not name:
                continue
            dst = dst_dir / name
            if dst.is_file():
                continue
            src = None
            for cand in registry._candidate_paths(spec, name):
                if cand != dst and cand.is_file():
                    src = cand
                    break
            if src is None:
                continue
            dst_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)          # NTFS 硬链接：同一份数据，两个名字
            except OSError:
                shutil.copy2(src, dst)     # 跨卷 / 权限失败时复制
            made += 1
            if progress:
                try:
                    progress(f"迁移模型 {spec.display}：{name}", None)
                except Exception:
                    pass
    return made


def installed_ids() -> list[str]:
    return [s.id for s in registry.MODELS if registry.is_installed(s)]


def cleanup_legacy(dry_run: bool = False) -> tuple[int, int, list[str]]:
    """删掉迁移后多余的旧目录副本，返回 (删除文件数, 字节数, 说明)。

    为什么能安全删：engine/models/<id>/<file> 是权威副本，旧路径那份要么是它的
    硬链接（同一个 inode），要么是当初 copy2 出来的同内容复制品。

    只删两种情况：
      1. 旧副本与 engine 副本 **是同一个 inode**（硬链接，删一个名字数据还在）；
      2. 旧副本与 engine 副本 **同名且大小一致**（复制品，engine 那份才是程序实际用的）；
    其它一律跳过（宁可留垃圾，不误删）。
    绝不删 engine.pid / engine.log（旧端口清理还要靠 pid 文件）。
    只删空目录，不 rmtree。

    ⚠️ dry_run=True 时第二项返回的是"旧副本路径合计大小"（供界面展示用），
    实际执行时返回的是"真正释放的字节数"（硬链接被删不释放空间，计 0）。
    """
    removed = 0
    freed = 0
    notes: list[str] = []
    emptied: list = []

    for spec in registry.MODELS:
        for name in (spec.file, spec.mmproj):
            if not name:
                continue
            base = registry.models_dir() / spec.id / name
            if not base.is_file():
                continue
            try:
                bst = base.stat()
            except OSError:
                continue
            for cand in registry._candidate_paths(spec, name):
                if cand == base or not cand.is_file():
                    continue
                try:
                    cst = cand.stat()
                except OSError:
                    continue
                same_inode = (cst.st_ino == bst.st_ino and cst.st_dev == bst.st_dev)
                same_copy = (cst.st_size == bst.st_size)
                if not (same_inode or same_copy):
                    notes.append(f"跳过（内容不一致）：{cand}")
                    continue
                if dry_run:
                    removed += 1
                    freed += cst.st_size        # 展示用：旧副本路径占用
                    continue
                try:
                    cand.unlink()
                    removed += 1
                    if not same_inode:
                        freed += cst.st_size    # 复制品才真正释放
                    emptied.append(cand.parent)
                except Exception as e:
                    notes.append(f"删除失败（可能被占用）：{cand.name} — {e}")

    # 顺手收掉变空的旧目录（只删空目录，非空不动）
    if not dry_run:
        for d in sorted(set(emptied), key=lambda p: -len(str(p))):
            cur = d
            for _ in range(3):          # 最多往上收 3 层
                try:
                    if cur.is_dir() and not any(cur.iterdir()):
                        cur.rmdir()
                        cur = cur.parent
                        continue
                except Exception:
                    pass
                break
    return removed, freed, notes
