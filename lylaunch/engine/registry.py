"""统一模型注册表 + preset INI 生成。

把原来散落在三处的 spec（doc_core / translator / chat）合并成一份 ModelDef。
id 沿用旧 id（= preset 段名 = 请求体 "model" 值），迁移零成本。

共 11 个模型：文档识别 2（xiaomi-ocr-0 / ovis-ocr2）、翻译 2（hy-mt2 1.8B / hy-mt2-7b）、
本地 AI 7（qwen3-4b / 1.7b / vl-8b / bonsai-25-27b / qwen35-9b-v4-distill /
lfm25-8b-a1b / deepseek-r1-14b）。
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
    ctx_max: int = 0              # 外部集成（长上下文）下的硬上限；0 = 跟随全局滑块
    kv_cache: str = ""            # "" | "q8_0" | "q4_0"：压 KV 换上下文，只用于外部那套
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
    # 详细设置页用的补充信息：谁做的、跑起来要多少内存
    org: str = ""
    ram: str = ""
    # 界面小标签（纯展示，配色由 settings_models._TAG_STYLE 决定）：
    # 只放「一句话能看懂、且与 caps 不重复」的信息，最多两个。
    tags: tuple[str, ...] = ()
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
        note="小米 2026 年 10 月开源的 0.8B 文档理解模型，基座是 Qwen3.5-0.8B。除了转文字，还能按字段抽发票 / 卡证信息、做 OCR 视觉问答；OmniDocBench v1.6 拿到 96.83，1B 以下排第一。",
        points=(
            "中文票据、截图、手写稿识别更稳",
            "整页转 Markdown，表格自动还原",
            "轻量快速，短页出字更快",
            "适合文字清晰的单页；扫描件建议 OvisOCR2",
        ),
        org="小米 · Xiaomi Research（SeerRay-Lab）",
        vram="约 1GB 显存",
        ram="建议 4GB 内存（纯 CPU 也能跑）",
        tags=("轻量",),
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
        note="混元 Hy-MT2 系列的 7B 中杯，2026 年 5 月开源，支持 33 种语言互译（含民汉）。会按术语表、分隔符、上下文、风格这些指令来翻，Flores200 / WMT25 上超过 DeepSeek-V4-Pro、Kimi K2.6 等更大的模型。",
        org="腾讯混元 · Tencent Hunyuan",
        vram="约 5GB 显存",
        ram="建议 8GB 内存",
        tags=("大显存",),
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
        note="阿里云 2026 年 7 月开源的 0.8B 端到端文档解析模型，同样后训练自 Qwen3.5-0.8B。一张图直接出 Markdown，不再拆「版面分析 + 内容识别」两段流水线；OmniDocBench v1.6 得 96.58，是端到端路线第一次登顶。",
        points=(
            "扫描件、图片型 PDF 输出干净 Markdown，无版面标签",
            "按阅读顺序，多栏论文、密集表格更准",
            "公式转 LaTeX，表格结构化还原",
            "长页上限 8K token，不易截断 / 少复读",
        ),
        org="阿里云 · ATH-MaaS（阿里巴巴集团）",
        vram="约 1GB 显存（0.8B，比 Xiaomi 更轻）",
        ram="建议 4GB 内存（纯 CPU 也能跑）",
        tags=("轻量",),
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
        note="混元 Hy-MT2 的轻量版。虽只有 1.8B，翻译质量却压过 Microsoft Translator、豆包翻译这些商业 API，同样支持 33 语种互译和术语表 / 风格指令；批量长文翻译时吞吐最划算。",
        org="腾讯混元 · Tencent Hunyuan",
        vram="约 1GB 显存",
        ram="建议 4GB 内存（纯 CPU 也能跑）",
        tags=("轻量",),
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
        note="阿里 Qwen3 系列的 4B 稠密模型（2025 年 4 月开源），可在「先想再答」和直答之间切换，覆盖 119 种语言，原生 32K 上下文。4B 是本地对话的甜点：8GB 显存还能留出上下文，速度和质量都不难受。",
        org="阿里 · Qwen 团队",
        vram="约 2.4GB 显存",
        ram="建议 6GB 内存",
        tags=("推荐", "会思考"),
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
        note="Qwen3 系列最小的一档（1.7B 稠密，2025 年 4 月），同样带思考模式、覆盖 119 种语言。核显或纯 CPU 就能跑，适合当常驻的轻量助手、分类打标；难题上会明显不如 4B。",
        org="阿里 · Qwen 团队",
        vram="约 1.1GB 显存",
        ram="建议 4GB 内存（纯 CPU 可跑）",
        tags=("低配友好", "CPU 可用"),
        legacy_dirs=("models/qwen3-1.7b/models",),
    ),
    ModelDef(
        id="qwen3-vl-8b",
        display="Qwen3-VL-8B",
        file="Qwen3VL-8B-Instruct-Q4_K_M.gguf", size=4795 * 1048576,
        mmproj="mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", mmproj_size=717 * 1048576,
        repos=("Qwen/Qwen3-VL-8B-Instruct-GGUF",),
        ctx=8192, ngl=99, jinja=True,
        # 112K 下 KV 要 16GB，12GB 卡直接 OOM（loader 报 failed to load）；
        # q8_0 压 KV 后 64K 实测可加载，96K 仍超。给它单独封顶，别跟着全局滑块一起炸。
        ctx_max=65536, kv_cache="q8_0",
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("vision", "chat"),
        note="Qwen3-VL 的 8B Instruct 版，原生视觉语言模型：看图、读表格、认截图里的字，支持 32 种语言 OCR。它比 OCR 专用模型更会「理解」图片，但输出格式不如 OCR 模型规整；原生支持 256K 上下文，12GB 卡上只给到 64K。",
        org="阿里 · Qwen 团队",
        vram="约 5.5GB 显存（12GB 卡更稳）",
        ram="建议 12GB 内存",
        tags=("能看图", "大显存"),
        legacy_dirs=("models/qwen3-vl-8b/models",),
    ),
    ModelDef(
        id="bonsai-25-27b",
        display="Ternary Bonsai 2.5 27B",
        short_name="Bonsai 2.5 27B",
        file="Ternary-Bonsai-2.5-27B-Q2_0.gguf", size=7673194848,
        mmproj="Ternary-Bonsai-2.5-27B-mmproj-Q8_0.gguf", mmproj_size=629246976,
        sha256="84496171ca79d4d33fb45612a2afd8303ec494baf916281023b7dc9cb3b3b9fa",
        repos=("prism-ml/Ternary-Bonsai-2.5-27B-GGUF",),
        # 只收 Q2_0：PTQ1_0 / PQ2_0 是 PrismML fork 的私有量化类型，标准 llama.cpp 直接拒载
        # （老构建甚至会「能加载但输出垃圾」）。Q2_0 走官方 b11556+ 的 Hadamard 路径，
        # 随包 runtime 是 b12047，llama.dll 里同时有 qwen35 与 hadamard，能加载。
        # 7.7GB 主模型 + 0.6GB 视觉投影，12GB 卡只敢给 8K；原生 262K 是给大显存准备的。
        ctx=8192, ctx_max=16384, ngl=99, jinja=True, reasoning="deepseek",
        sampling={"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                  "min_p": 0, "max_tokens": 4096},
        caps=("vision", "chat", "thinking"),
        license="Apache-2.0",
        note="PrismML 2026 年 11 月发布的 27B「三值」模型：权重只取 -1 / 0 / +1，5.9GB 就装下 27B，还能看图、调工具。智能体编程是强项（SWE-bench Verified 75.6），但思考默认 xhigh，出字慢，简单问题容易想太久。",
        org="PrismML（基于 Qwen3.8 27B 三值量化）",
        vram="约 8.3GB 显存（含视觉投影，8K 上下文）",
        ram="建议 12GB 内存（100K 上下文约 14.7GB）",
        tags=("能看图", "省显存"),
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
        note="社区作者 Jackrong 用 DeepSeek-V4-Flash 的推理数据（8000 条蒸馏集）微调 Qwen3.5-9B 得到。保留 Qwen3.5 的原生多模态与 262K 上下文，思路更像 DeepSeek：先写思维链再答，简单题偶尔想过头。",
        org="Jackrong（社区蒸馏，基座 Qwen3.5-9B）",
        vram="约 7.5GB 显存（16K 上下文）",
        ram="建议 12GB 内存",
        tags=("能看图", "会思考"),
        legacy_dirs=("models/qwen35-9b-v4-distill/models",),
    ),
    ModelDef(
        id="lfm25-8b-a1b",
        display="LFM2.5-8B-A1B",
        short_name="LFM2.5 8B",
        file="LFM2.5-8B-A1B-UD-Q4_K_M.gguf", size=5322223200,
        sha256="e2c8350d5e6e7c633b2ba4e20805a2c86bc46884cd285e8f3fe36ad917b772c7",
        repos=("unsloth/LFM2.5-8B-A1B-GGUF",),
        # 24 层里只有 6 层 GQA，KV 开销很小 → 插件这套给到 32K 也不吃显存；
        # 原生 128K 只留给外部集成（那里的硬上限靠 ctx_max 拦）。
        ctx=32768, ctx_max=131072, ngl=99, jinja=True, reasoning="deepseek",
        sampling={"temperature": 0.2, "top_k": 80, "repeat_penalty": 1.05,
                  "max_tokens": 4096},
        caps=("text", "chat", "thinking"),
        license="LFM Open License v1.0",
        note="Liquid AI 2026 年 5 月的端侧 MoE：8.3B 总参数、每个 token 只激活 1.5B，128K 上下文，主打工具调用和复杂指令遵循，同尺寸里吞吐最快。纯文本，且强制思维链（关不掉），每次回答都要先想一段。",
        org="Liquid AI",
        vram="约 5.3GB 显存",
        ram="建议 8GB 内存（32K 上下文约 6GB）",
        tags=("省显存", "会思考"),
        legacy_dirs=("models/lfm25-8b-a1b/models",),
    ),
    ModelDef(
        id="deepseek-r1-14b",
        display="DeepSeek-R1-14B",
        file="DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf", size=8572 * 1048576,
        repos=("unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF",),
        ctx=4096, ngl=99, jinja=True, reasoning="deepseek",
        sampling={"temperature": 0.7, "max_tokens": 1024},
        caps=("text", "chat", "thinking"),
        note="DeepSeek-R1 的 14B 蒸馏版（R1-Distill-Qwen-14B），2025 年 1 月随 R1 一起开源：用 R1 生成的推理轨迹训练 Qwen-14B。数学和代码推理明显强于同尺寸通用模型，代价是先写一大段思考；14B 在 12GB 卡上偏紧，上下文只能给 4K。",
        org="DeepSeek（R1 蒸馏到 Qwen-14B）",
        vram="约 8.6GB 显存（4K 上下文，偏紧）",
        ram="建议 16GB 内存",
        tags=("会思考", "大显存"),
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
    """插件那套 Router 的 preset。"""
    return engine_dir() / "models.ini"


def external_preset_path() -> Path:
    """外部集成（dsh）那套 Router 的 preset：独立文件，两套 ctx 互不干扰。"""
    return engine_dir() / "models-ext.ini"


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
def effective_ctx(spec: ModelDef, external: bool = False) -> int:
    """该模型实际使用的上下文窗口。

    两套 Router 各用各的：
    - **外部集成那套**（``external=True``）取设置页滑块的全局档。dsh 的 agent
      系统提示 + 工具描述就要约 9.4K tokens，出厂 16K 里塞不下多少对话，
      所以这个值必须能调大，且与写进 dsh 的 ``contextWindow`` 同源。
    - **插件那套**用模型自己的出厂值：翻译 / OCR / 本地 AI 的请求都短，
      跟着调到 64K 只会白吃 KV cache，还可能让加载失败。
    """
    if external:
        try:
            from .. import config
            ctx = int(config.load_engine_context())
        except Exception:
            return int(spec.ctx)
        # 全局档位由用户按显存拉，但每个模型的 KV 开销差得远：VL-8B 每 token
        # 约 140KB，112K 就要 16GB。这里按模型自己的天花板截一刀，避免「设了大
        # 上下文，结果把某个模型加载成 OOM」——那种 500 报错离原因很远，极难查。
        if spec.ctx_max:
            return min(ctx, int(spec.ctx_max))
        return ctx
    return int(spec.ctx)


def render_preset(external: bool = False) -> str:
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
        lines.append(f"ctx-size = {effective_ctx(spec, external)}")
        lines.append(f"n-gpu-layers = {spec.ngl}")
        lines.append(f"jinja = {'true' if spec.jinja else 'false'}")
        # KV 量化只给外部那套用：插件（翻译 / OCR / 本地 AI）窗口本来就小，
        # 压 KV 换不来空间，只会平白引入精度风险。
        if external and spec.kv_cache:
            lines.append(f"cache-type-k = {spec.kv_cache}")
            lines.append(f"cache-type-v = {spec.kv_cache}")
        if spec.reasoning:
            lines.append(f"reasoning-format = {spec.reasoning}")
        lines.append("load-on-startup = false")
        lines.append("stop-timeout = 10")
        lines.append("")
    return "\n".join(lines)


def write_preset(external: bool = False) -> Path:
    """把 preset 写盘（每次启动对应那套 router 前重写）。"""
    p = external_preset_path() if external else preset_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_preset(external), encoding="utf-8")
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
