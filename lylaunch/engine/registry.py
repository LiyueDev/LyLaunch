"""统一模型注册表 + preset INI 生成。

把原来散落在三处的 spec（doc_core / translator / chat）合并成一份 ModelDef。
id 沿用旧 id（= preset 段名 = 请求体 "model" 值），迁移零成本。

共 9 个模型：文档识别 2（xiaomi-ocr-0 / ovis-ocr2）、翻译 2（hy-mt2 1.8B / hy-mt2-7b）、
本地 AI 5（qwen3-4b / 1.7b / vl-8b / qwen35-9b-v4-distill / deepseek-r1-14b）。
旧 qwen2.5-0.5B 翻译模型已移除（0.5B 能力不足，中译外几乎不翻）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 训练期固定 prompt
# ⚠️ 别改写成中文短句：2026-11-14 实测掉质量（表格拍平、版面标签乱飞）。
PROMPT_XIAOMI = (
    "Extract all information from the main body of the document image and represent it "
    "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
    "format, formulas in the document should be represented using LATEX format, and the "
    "parsing should be organized according to the reading order."
)
PROMPT_OVISOCR2 = (
    "Extract all readable content from the image in natural human reading order "
    "and output the result as a single Markdown document. "
    "For charts or images, represent them using an HTML image tag: "
    '<img src="images/bbox_left_top_right_bottom.jpg" />, where left, top, right, bottom '
    "are bounding box coordinates scaled to [0, 1000). "
    "Format formulas as LaTeX. Format tables as HTML: <table>...</table>. "
    "Transcribe all other text as standard Markdown. "
    "Preserve the original text without translation or paraphrasing."
)


@dataclass(frozen=True)
class ModelDef:
    # 身份
    id: str
    display: str
    short_name: str = ""
    # 文件
    file: str = ""
    size: int = 0                 # 精确字节数；0 = 以 Content-Length 为准
    mmproj: str = ""
    mmproj_size: int = 0
    repos: tuple[str, ...] = ()
    sha256: str = ""
    # 启动参数（进 preset INI）
    ctx: int = 8192
    ngl: int = 99
    jinja: bool = True
    reasoning: str = ""           # "" | "deepseek" → reasoning-format
    # 请求默认采样（进请求体，不进 INI）
    sampling: dict = field(default_factory=dict)
    # 能力与文案
    caps: tuple[str, ...] = ()
    prompt: str = ""
    dialect: str = ""
    license: str = ""
    note: str = ""
    points: tuple[str, ...] = ()
    vram: str = ""
    # 迁移：相对 %LocalAppData%\LyLaunch 的旧目录（P2 硬链接用）
    legacy_dirs: tuple[str, ...] = ()

    @property
    def total_mb(self) -> int:
        return round((self.size + self.mmproj_size) / 1048576)

    # ---- 兼容旧 ModelSpec 的属性名（doc_engine / 插件沿用） ----
    @property
    def name(self) -> str:
        return self.display

    @property
    def max_tokens(self) -> int:
        return int(self.sampling.get("max_tokens", 1024))

    @property
    def repo(self) -> str:
        return self.repos[0] if self.repos else ""

    @property
    def size_mb(self) -> int:
        return round(self.size / 1048576)

    @property
    def mmproj_size_mb(self) -> int:
        return round(self.mmproj_size / 1048576)


# P1：2 个对撞性最强的模型（本机均已安装）
MODELS: list[ModelDef] = [
    ModelDef(
        id="xiaomi-ocr-0",
        display="Xiaomi-OCR-0",
        file="Xiaomi-OCR-0.Q4_K_M.gguf", size=529297664,
        mmproj="Xiaomi-OCR-0.mmproj-bf16.gguf", mmproj_size=207346624,
        # 0.39 重构时误写成官方仓库（ModelScope 上不存在 → 404，下载必失败）；
        # 实际 GGUF 发布在 prithivMLmods（2026-11-14 21:2x 实测 200）。官方仓库留作回退。
        repos=("prithivMLmods/Xiaomi-OCR-0-GGUF", "xiaomi-research/Xiaomi-OCR-0-GGUF"),
        ctx=16384, ngl=99, jinja=True,
        sampling={"temperature": 0, "top_p": 0.8, "repeat_penalty": 1.05, "max_tokens": 4096},
        caps=("vision", "ocr"),
        prompt=PROMPT_XIAOMI, dialect="xiaomi",
        note="中文票据 / 截图 / 手写更稳 · 轻量快速",
        points=(
            "中文票据、截图、手写稿识别更稳",
            "整页转 Markdown，表格自动还原",
            "轻量快速，短页出字更快",
            "适合文字清晰的单页；扫描件建议 OvisOCR2",
        ),
        vram="约 1GB 显存",
        legacy_dirs=("models/xiaomi-ocr-0/models",),
    ),
    ModelDef(
        id="hy-mt2-7b",
        display="Hy-MT2 7B",
        file="Hy-MT2-7B-Q4_K_M.gguf", size=4960000000,
        repos=("Tencent-Hunyuan/Hy-MT2-7B-GGUF",),
        ctx=8192, ngl=99, jinja=True,
        sampling={"temperature": 0.7, "top_p": 1.0, "top_k": -1,
                  "repeat_penalty": 1.0, "max_tokens": 4096},
        caps=("text", "translate"),
        license="Apache-2.0",
        note="高质量翻译 · 长句、专业内容、多语言最稳",
        vram="约 5GB 显存",
        legacy_dirs=("models/llm/models",),
    ),
    ModelDef(
        id="ovisocr2",
        display="OvisOCR2",
        file="OvisOCR2-Q4_K_M.gguf", size=529297280,
        mmproj="mmproj-F16.gguf", mmproj_size=204987079,
        repos=("Abiray/OvisOCR2-GGUF",),
        ctx=16384, ngl=99, jinja=True,
        sampling={"temperature": 0, "top_p": 0.8, "repeat_penalty": 1.05, "max_tokens": 8192},
        caps=("vision", "ocr"),
        prompt=PROMPT_OVISOCR2, dialect="ovisocr2",
        license="Apache 2.0",
        note="扫描件 / 图片型 PDF 首选 · 输出干净",
        points=(
            "扫描件、图片型 PDF 输出干净 Markdown，无版面标签",
            "按阅读顺序，多栏论文、密集表格更准",
            "公式转 LaTeX，表格结构化还原",
            "长页上限 8K token，不易截断 / 少复读",
        ),
        vram="约 1GB 显存（0.8B，比 Xiaomi 更轻）",
        legacy_dirs=("models/xiaomi-ocr-0/models",),
    ),
    ModelDef(
        id="hy-mt2",
        display="Hy-MT2 1.8B",
        file="Hy-MT2-1.8B-Q4_K_M.gguf", size=1133080448,
        sha256="dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
        repos=("Tencent-Hunyuan/Hy-MT2-1.8B-GGUF", "vipnet/Hy-MT2-1.8B-GGUF"),
        ctx=8192, ngl=99, jinja=True,
        sampling={"temperature": 0.7, "top_p": 0.6, "top_k": 20,
                  "repeat_penalty": 1.05, "min_p": 0},
        caps=("text", "translate"),
        license="Apache-2.0",
        note="翻译专用 · 质量高",
        vram="约 1GB 显存",
        legacy_dirs=("models/llm/models",),
    ),
    ModelDef(
        id="qwen3-4b",
        display="Qwen3-4B",
        file="Qwen3-4B-Q4_K_M.gguf", size=2382 * 1048576,
        repos=("Qwen/Qwen3-4B-GGUF",),
        ctx=8192, ngl=99, jinja=True,
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("text", "chat", "thinking"),
        note="推荐 · 8GB 显存甜点，日常够用",
        vram="约 2.4GB 显存",
        legacy_dirs=("models/qwen3-4b/models",),
    ),
    ModelDef(
        id="qwen3-1.7b",
        display="Qwen3-1.7B",
        file="Qwen3-1.7B-Q4_K_M.gguf", size=1056 * 1048576,
        repos=("unsloth/Qwen3-1.7B-GGUF",),
        ctx=8192, ngl=99, jinja=True,
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("text", "chat", "thinking"),
        note="最轻 · 核显/CPU 也能跑",
        vram="约 1.1GB 显存",
        legacy_dirs=("models/qwen3-1.7b/models",),
    ),
    ModelDef(
        id="qwen3-vl-8b",
        display="Qwen3-VL-8B",
        file="Qwen3VL-8B-Instruct-Q4_K_M.gguf", size=4795 * 1048576,
        mmproj="mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", mmproj_size=717 * 1048576,
        repos=("Qwen/Qwen3-VL-8B-Instruct-GGUF",),
        ctx=8192, ngl=99, jinja=True,
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("vision", "chat"),
        note="能看图 · 文档/表格/截图理解，需 12GB 显存",
        vram="约 5.5GB 显存",
        legacy_dirs=("models/qwen3-vl-8b/models",),
    ),
    ModelDef(
        id="qwen35-9b-v4-distill",
        display="Qwen3.5-9B-DeepSeek-V4-Flash",
        short_name="Qwen3.5-9B",
        file="Qwen3.5-9B-DeepSeek-V4-Flash-Q4_K_M.gguf", size=5368 * 1048576,
        mmproj="mmproj.gguf", mmproj_size=879 * 1048576,
        repos=("Jackrong/Qwen3.5-9B-DeepSeek-V4-Flash-GGUF",),
        ctx=16384, ngl=99, jinja=True, reasoning="deepseek",
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("vision", "chat", "thinking"),
        note="底座 Qwen3.5-9B，用 DeepSeek-V4-Flash 推理数据训练；会思考、也能看图",
        vram="约 7.5GB 显存（16K 上下文）",
        legacy_dirs=("models/qwen35-9b-v4-distill/models",),
    ),
    ModelDef(
        id="deepseek-r1-14b",
        display="DeepSeek-R1-14B",
        file="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", size=8572 * 1048576,
        repos=("unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF",),
        ctx=4096, ngl=99, jinja=True, reasoning="deepseek",
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("text", "chat", "thinking"),
        note="推理强 · 会先思考再答（慢）；12GB 显存偏紧，上下文压到 4096",
        vram="约 8.6GB 显存（偏紧）",
        legacy_dirs=("models/deepseek-r1-14b/models",),
    ),
]

MODEL_BY_ID: dict[str, ModelDef] = {m.id: m for m in MODELS}
DEFAULT_MODEL_ID = "xiaomi-ocr-0"


def spec_of(mid: str | None) -> ModelDef:
    """脏值/空值安全回退，别让界面上一个坏配置就起不来。"""
    return MODEL_BY_ID.get(mid or "", MODELS[0])


def by_cap(cap: str) -> list[ModelDef]:
    return [m for m in MODELS if cap in m.caps]


# ---------------------------------------------------------------- 路径解析
def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "LyLaunch"


def engine_dir() -> Path:
    return _data_dir() / "engine"


def models_dir() -> Path:
    return engine_dir() / "models"


def preset_path() -> Path:
    return engine_dir() / "models.ini"


def _candidate_paths(spec: ModelDef, name: str) -> list[Path]:
    """按优先级返回某个文件的候选路径：统一目录优先，旧目录回退（P2 迁移前靠回退）。"""
    if not name:
        return []
    cands = [models_dir() / spec.id / name]
    for d in spec.legacy_dirs:
        cands.append(_data_dir() / d / name)
    return cands


def model_file_path(spec: ModelDef) -> Path | None:
    for p in _candidate_paths(spec, spec.file):
        if p.is_file():
            return p
    return None


def mmproj_path(spec: ModelDef) -> Path | None:
    if not spec.mmproj:
        return None
    for p in _candidate_paths(spec, spec.mmproj):
        if p.is_file():
            return p
    return None


def is_installed(spec: ModelDef) -> bool:
    if not model_file_path(spec):
        return False
    if spec.mmproj and not mmproj_path(spec):
        return False
    return True


def model_files_problem(spec: ModelDef) -> str:
    """返回「文件不能交给 router 加载」的原因；空串表示可以加载。

    和 is_installed() 的差别：这里额外查**明显残缺**（拷贝/下载中断留下半截 gguf）。
    只做下限判断（< 登记大小的 95%），因为多数 spec.size 是按 MB 估算的近似值，
    跟真实字节数差几十 KB~1MB，不能精确相等比较；9 个模型里也只有 1 个登记了 sha256。

    典型场景：新电脑装完包一个模型都没下载，preset 里却已经写了 9 个段，
    router 会真的去加载那个不存在的路径 → 子进程 `exiting due to model loading error`。
    """
    mf = model_file_path(spec)
    if mf is None:
        return f"缺少主模型文件 {spec.file}（尚未下载）"
    try:
        actual = mf.stat().st_size
    except OSError:
        return f"主模型文件 {spec.file} 无法读取"
    if spec.size and actual < int(spec.size * 0.95):
        return (f"主模型文件不完整（实有 {actual / 1048576:.0f} MB，"
                f"应约 {spec.size / 1048576:.0f} MB），请删除后重新下载")
    if spec.mmproj:
        mp = mmproj_path(spec)
        if mp is None:
            return f"缺少视觉投影文件 {spec.mmproj}（尚未下载完整）"
        try:
            m_actual = mp.stat().st_size
        except OSError:
            return f"视觉投影文件 {spec.mmproj} 无法读取"
        if spec.mmproj_size and m_actual < int(spec.mmproj_size * 0.95):
            return (f"视觉投影文件不完整（实有 {m_actual / 1048576:.0f} MB，"
                    f"应约 {spec.mmproj_size / 1048576:.0f} MB），请删除后重新下载")
    return ""


# ---------------------------------------------------------------- preset INI
def effective_ctx(spec: ModelDef) -> int:
    """该模型实际使用的上下文窗口：用户在「外部集成」页选的全局档。

    出厂值（spec.ctx）只是各模型自己的默认，不做上限——用户为了让 dsh 这类
    外部工具跑得动而统一调大时，工厂默认会把请求挡回去（9B 的 16K 上限根本
    放不下 dsh 约 9.4K 的系统提示 + 4096 output）。preset 与 dsh 的
    contextWindow 必须同取这一个数，否则引擎按 32K 加载、dsh 按 16K 算预算，
    请求还是会被拒。
    """
    try:
        from .. import config
        return int(config.load_engine_context())
    except Exception:
        return int(spec.ctx)


def render_preset() -> str:
    """生成 --models-preset 的 INI。

    硬约束（踩过的坑，别改）：
    1. 首行必须 `version = 1`，否则整份 preset 被忽略；
    2. 段名 = 模型 id（= 请求体 model 值）；
    3. 绝不写 host/port/api-key（router 会剥掉，写了反而误导）。

    0.39.5：**只声明文件真的存在的模型**。router 启动时会为 preset 里每个段
    测显存，没下载的模型必然刷 `error loading model`（还会让「别的模型缺文件」
    看起来像当前模型加载失败）。一个都没装时退回全量，保证 router 能起来。
    """
    segs = [s for s in MODELS if is_installed(s)] or list(MODELS)
    lines: list[str] = ["version = 1", ""]
    for spec in segs:
        mf = model_file_path(spec) or (models_dir() / spec.id / spec.file)
        lines.append(f"[{spec.id}]")
        lines.append(f"model = {mf}")
        mp = mmproj_path(spec)
        if mp is not None:
            lines.append(f"mmproj = {mp}")
        lines.append(f"ctx-size = {effective_ctx(spec)}")
        lines.append(f"n-gpu-layers = {spec.ngl}")
        lines.append(f"jinja = {'true' if spec.jinja else 'false'}")
        if spec.reasoning:
            lines.append(f"reasoning-format = {spec.reasoning}")
        lines.append("load-on-startup = false")
        lines.append("stop-timeout = 10")
        lines.append("")
    return "\n".join(lines)


def write_preset() -> Path:
    """把 preset 写到 engine/models.ini（每次启动 router 前重写）。"""
    p = preset_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_preset(), encoding="utf-8")
    return p


def remove_files(spec_or_id) -> tuple[bool, str]:
    """删除某模型的所有本地副本（engine/models + 旧目录硬链接），彻底卸载。

    关键：P2 迁移用硬链接把旧目录模型链进 engine/models，同一份文件有两个名字。
    只删 engine 那份，is_installed() 会回退旧目录仍判定"已安装"→ 看起来没卸载。
    因此这里把所有候选路径逐个 unlink（不 rmtree 共享旧目录，避免误删同目录其它模型）。
    """
    spec = spec_of(spec_or_id) if isinstance(spec_or_id, str) else spec_or_id
    removed = 0
    for name in (spec.file, spec.mmproj):
        if not name:
            continue
        for p in _candidate_paths(spec, name):
            for _ in range(3):
                try:
                    if p.is_file():
                        p.unlink()
                        removed += 1
                    break
                except PermissionError:
                    time.sleep(0.5)
                except Exception:
                    break
    # 清理 engine 专属空目录（旧共享目录不动）
    d = models_dir() / spec.id
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except Exception:
        pass
    ok = not is_installed(spec)
    if ok:
        return True, f"已卸载 {spec.display}（删除 {removed} 个文件）"
    return False, f"{spec.display} 卸载失败，可能仍被占用，请重启后重试"
