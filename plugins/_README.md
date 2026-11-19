# LyLaunch 插件系统 · 开发说明

> **本文件 = 插件开发的权威约定文档（单一事实来源）。**
> 新增 / 修改插件请以本文件为准，它与 `liytools/plugins/` 现有代码共同构成插件开发约定。
> 插件系统与启动器基础功能（搜索/剪贴板/启动）**完全隔离**。
> 本目录 `plugins/` 是**内置插件**源目录（随程序分发）；用户安装的插件在
> `%LocalAppData%\LyLaunch\plugins\`。

> 🔒 **Qt 绑定（0.37 起）**：插件 UI 一律 `from PySide6.QtWidgets import ...`；**PyQt6 已全面弃用**。本文件里出现的 `PyQt6` 只在标注了「0.36 历史」的说明里，别照抄。

## 一、插件长什么样

一个插件 = 一个目录，目录名 = 插件 Id（必须一致），里面至少两个文件：

```
plugins/<插件Id>/
├── plugin.json        # 插件清单（身份证）
└── main.py            # 插件代码（入口脚本）
```

参考示例：`plugins/ocr/`（内置 OCR 插件，纯离线，可直接抄它的结构）。

## 二、plugin.json 字段（插件清单）

| 字段 | 说明 | 示例 |
|---|---|---|
| `Id` | 唯一标识，**必须等于目录名**（小写+下划线） | `"ocr"` |
| `Name` | 显示名称（首页瓦片/设置页显示） | `"文字识别 OCR"` |
| `Version` | 版本号，软件内检查更新用 | `"0.1.0"` |
| `Author` | 作者 | `"Liy"` |
| `Description` | 一句话介绍 | `"点一下弹个窗"` |
| `Icon` | 图标文件名（相对插件目录），留空用首字母瓦片 | `"icon.png"` |
| `Main` | 入口脚本文件名 | `"main.py"` |
| `EntryClass` | 入口脚本里继承 `PluginBase` 的类名 | `"Plugin"` |
| `Keywords` | 触发关键词（中文/英文/拼音别名，进搜索索引） | `["示例", "hello", "demo"]` |
| `Permissions` | 申请的系统能力（见下） | `["notification"]` |
| `Hotkey` | 全局快捷键（可选）。主程序在窗口显示后注册，按下即执行本插件 `on_execute`；空 = 不注册。写法 `"Ctrl+L"` / `"Alt+Space"` / `"Ctrl+Shift+F1"` | `"Ctrl+L"` |
| `Config` | 插件自己的默认配置，运行期可改并持久化 | `{"greeting": "你好"}` |

**Permissions 可取值**（能力申请，v1 已实现）：
- `clipboard` —— 读写剪贴板
- `notification` —— 托盘气泡通知
- `config` —— 读写插件自身持久化配置

## 三、代码怎么写（PluginBase 契约）

```python
from liytools.plugins.base import PluginBase

class Plugin(PluginBase):
    def on_load(self) -> None:
        """插件被加载后调用（做初始化）。"""

    def on_unload(self) -> None:
        """卸载/禁用/退出前调用（释放资源）。"""

    def on_keyword(self, keyword: str, query: str) -> list:
        """搜索命中关键词时调用（v2 实现搜索接管，先留接口）。"""

    def on_execute(self, query: str = "") -> None:
        """用户点中插件时执行。query 是当前搜索框文字。
        ★ 必须"做完即返回"：开窗口一律非模态 show() + 模块级单例持有引用，
        禁止 dlg.exec()（会锁死整个主程序，见第十章头号红线）。"""

    def widget(self):
        """返回插件的 Qt 界面；None 表示无常驻界面（v2 实现页面切换）。"""
        return None
```

### PluginContext（插件"环境"，构造时由管理器注入，子类里叫 `self.ctx`）

| 成员 | 说明 |
|---|---|
| `ctx.api` | `PluginAPI` 实例——系统能力入口（见下表） |
| `ctx.manifest` | 本插件清单（`plugin.json` 解析结果，`PluginManifest`） |
| `ctx.plugin_dir` | 本插件目录绝对路径（放资源就 relative 到这里） |
| `ctx.settings` | 插件自己的配置 dict（初始 = 清单里的 `Config` 字段） |
| `ctx.id` / `ctx.name` | 插件 Id / 显示名 |
| `ctx.resource("icon.png", ...)` | 拼插件目录内资源文件的绝对路径 |

### PluginAPI 完整能力表（`liytools/plugins/api.py`，插件能碰系统的**唯一边界**）

> 每个方法都是幂等、容错的（失败返回默认值/False/静默），插件里不需要 try/except 兜底。
> 新增系统能力 = 改 `api.py`（对所有插件生效）。

| 方法 | 说明 | 权限 |
|---|---|---|
| `clipboard_read() -> str` | 读剪贴板文本（无则空串） | `clipboard` |
| `clipboard_write(text: str)` | 写文本进剪贴板（经主程序 Watcher，**不污染**剪贴板历史） | `clipboard` |
| `clipboard_write_image(image)` | 写图片进剪贴板（`QPixmap`/`QImage`，截图类插件用） | `clipboard` |
| `settings_get(key, default=None)` | 读插件自己的持久化配置项 | `config` |
| `settings_set(key, value)` | 写插件自己的配置项并**立即落盘**（`%LocalAppData%\LyLaunch\plugins_<Id>.json`） | `config` |
| `settings_save()` | 手动强制落盘（一般不需要） | `config` |
| `launch(path_or_url, args=None) -> bool` | 启动应用/文件/URL；`args` 给带参系统应用 | — |
| `reveal(path) -> bool` | 资源管理器定位文件/目录 | — |
| `notify(title, message="")` | 托盘气泡通知（3 秒） | `notification` |
| `invoke_plugin(plugin_id, query="") -> bool` | 调用另一个插件的 `on_execute(query)`（经 PluginManager 路由，插件间**不直接 import**；目标未装/禁用/抛错返回 False） | — |

> `Permissions` 目前是"声明式"（进清单即可），实际拦截在后续安全沙箱（v2）里做；现有内置插件均按需声明。

## 四、配置文件（三份，都在 %LocalAppData%\LyLaunch\）

| 文件 | 作用 | 谁来写 |
|---|---|---|
| `plugin_config.json` | 插件系统全局配置：总开关/扫描路径/禁用列表 | 设置页/用户 |
| `plugins_installed.json` | 已安装注册表：装了哪些、启没启用 | 管理器（装/卸载时） |
| `plugins_<Id>.json` | 每个插件自己的配置（插件用 api 读写） | 插件自己 |

`plugin_config.json` 完整字段见 `liytools/plugins/config.py` 的 `PluginSystemConfig`：
- `Enabled` 插件系统总开关（False 完全不加载）
- `AutoLoad` 启动时自动加载已启用插件
- `AllowExternal` 是否允许安装外部插件
- `ScanPaths` 额外插件扫描目录（数组）
- `DisabledPlugins` 手动禁用的插件 Id（数组）
- `MaxPlugins` 插件数量上限
- `HotkeyScope` / `NetworkAllowed` 后续能力预留开关

## 五、开发流程（三步）

1. 在 `plugins/` 下建目录 `<你的插件Id>/`，写 `plugin.json` + `main.py`
2. 启动 `python main.py`，插件系统会自动扫描到它（设置页能看到）
3. 在首页"插件"区块点它，`on_execute` 生效

> 卸载/启用/禁用由设置页的"插件管理"操作，不需要手改注册表。

## 六、下次怎么加插件（对接约定）

- **怎么说**：新会话里直接讲插件需求即可，例如「给 LyLaunch 加个 XX 插件」「把 ocr 改成 XX」。助手接手流程：
  先读本文件（权威约定）→ 看 `liytools/plugins/` 现有代码 → 按下面模板写。
- **两种加插件方式**：
  1. **内置插件**（随程序分发，进版本库）：在仓库 `plugins/<Id>/` 下建目录 + 文件，提交即生效。
  2. **用户安装插件**（不进版本库）：打成 zip，在设置页「插件管理 → 安装」导入，落到 `%LocalAppData%\LyLaunch\plugins/`。
- **插件 Id 是硬约束**：目录名 = `plugin.json` 里的 `Id`（小写+下划线），不一致扫描直接跳过。
- **能力声明前置**：插件要用的系统能力（剪贴板/通知/配置）必须先写进 `Permissions`；没声明的能力 `PluginAPI` 不开放。

## 七、最小插件模板（直接抄）

`plugins/<你的Id>/plugin.json`：
```json
{
  "Id": "my_plugin",
  "Name": "我的插件",
  "Version": "0.1.0",
  "Author": "LiyueDev",
  "Description": "一句话介绍",
  "Icon": "",
  "Main": "main.py",
  "EntryClass": "Plugin",
  "Keywords": ["我的", "my", "wode"],
  "Permissions": ["notification", "config"],
  "Config": { "greeting": "你好" }
}
```

`plugins/<你的Id>/main.py`：
```python
from liytools.plugins.base import PluginBase
from PySide6.QtWidgets import QMessageBox

class Plugin(PluginBase):
    def on_load(self) -> None:
        # 插件加载时初始化（可选）
        pass

    def on_execute(self, query: str = "") -> None:
        # 点瓦片时执行；query 是当前搜索框文字
        greeting = self.settings.get("Config", {}).get("greeting", "你好")
        QMessageBox.information(None, "我的插件", f"{greeting}\n你输入了：{query}")
        # ↑ 短时确认框用 information()/exec() 可以（点一下就关）。
        # ↓ 但如果是常驻主窗口，必须用非模态 show() + 单例，见第十章头号红线：
        # _open_nonmodal(MyDialog, self.ctx.api, self.ctx.settings)

    def on_unload(self) -> None:
        # 卸载/禁用/退出前释放资源（可选）
        pass
```
> 注：`self.settings` 是插件清单里的 `Config` 初值；运行时改配置用 `ctx.api.settings_set/get`（持久化到 `plugins_<Id>.json`）。

## 八、常见错误排查

| 现象 | 原因 | 修法 |
|---|---|---|
| 首页看不到插件 | 目录名 ≠ `plugin.json` 的 `Id` | 两者改一致（小写+下划线） |
| 点瓦片报错 / 没反应 | `EntryClass` 名字拼错或 `Main` 文件名错 | 类名/文件名与清单一致 |
| 插件没出现在设置页 | 目录不在 `plugins/`（内置）或没装到用户目录 | 放对位置或走安装 |
| 读不到剪贴板/通知 | `Permissions` 没声明对应能力 | 在清单 `Permissions` 加上 `clipboard`/`notification`/`config` |
| 卸载后还显示 | 内置插件禁止物理删除，只禁用 | 设置页点"禁用"即可 |
| 改了代码不生效 | 没重启程序（插件加载在启动时） | 重启 `python main.py` |

## 九、隔离边界（重申）

- 插件**只能**通过 `PluginAPI`（`ctx.api`）碰系统能力；**不得** `import` 主窗口 / 设置页 / 基础模块。
- 主窗口**只**通过 `PluginManager` 拿插件数据（`get_plugin_items()` / `execute()`），**不直接** `import` 插件代码。
- 加系统能力 = 改 `liytools/plugins/api.py`（对所有插件生效）；改插件契约 = 改 `liytools/plugins/config.py` 的 dataclass。
- 上述边界是"与基础功能隔离"的核心约定，后续加插件时务必遵守。

## 十、插件 UI 开发经验（内置插件踩过的坑，新插件可照抄）

> **★ 头号红线（2026-11-05 修了 4 个插件的真实事故）：插件主窗口禁止 `dlg.exec()`。**
> `QDialog.exec()` 是**模态**的，会启动一个嵌套事件循环并一直阻塞到窗口关闭——
> 插件窗口开着时，**整个 LyLaunch 主程序（搜索框、热键、托盘）全部无响应**。
> 曾中招的 4 个插件：`llm_chat`（本地 AI）、`ocr`（文字识别）、`translator`（文本翻译）、
> `doc_recognize`（文档识别），都是 `dlg = XxxDialog(...); dlg.exec()` 这一行。
>
> **正确写法（每个插件模块各写一份，见 `plugins/ocr/main.py` 的 `_open_nonmodal`）：**
> ```python
> _DLG = None   # 模块级单例：持有引用防 GC + 重复打开只提到前台
>
> def _open_nonmodal(cls, api, settings, **kw) -> None:
>     global _DLG
>     if _DLG is not None:
>         try:
>             _DLG.raise_(); _DLG.activateWindow(); return
>         except RuntimeError:
>             _DLG = None          # 底层对象已销毁
>     dlg = cls(api, settings, **kw)
>     dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)   # 关闭即销毁
>     dlg.destroyed.connect(lambda *_: globals().__setitem__("_DLG", None))
>     _DLG = dlg
>     dlg.show()                  # ★ 非模态：on_execute 立即返回，主程序继续可用
>     dlg.raise_(); dlg.activateWindow()
> ```
> 三个要点缺一不可：
> 1. **`show()` 不是 `exec()`**——否则 `on_execute` 不返回，主程序被锁死。
> 2. **模块级变量持有引用**——`dlg` 若只是局部变量，函数返回后 Python 对象被回收，
>    窗口会立刻消失或崩溃（`Qt.Popup` 顶层窗口同理，见 `llm_chat` 的 `self._pop`）。
> 3. **`WA_DeleteOnClose` + `destroyed` 清引用**——否则关闭后 `_DLG` 还指向已销毁对象，
>    下次打开会 `RuntimeError`；同时保证 `closeEvent()` 里的清理（停 worker / 释放引擎）照常执行。
>
> **哪些 `exec()` 是允许的（别一刀切误伤）**：
> - `ConfirmDialog.exec()` 之类的**短时确认框**：用户点一下就关，不是常驻主窗口
>   （`llm_chat` 的删除/重命名确认、Popconfirm 都用这个，保留）。
> - `QEventLoop.exec()` 的**有明确退出条件的短时同步等待**：如截图插件等用户框选完、
>   翻译插件首次下载离线模型（`finished`/`failed`/取消 都会 `loop.quit()`）。
>   判定标准是「会不会一直阻塞到用户主动关掉这个窗口」——会，就是主窗口，必须改。
>
> **跨插件传值不能再靠 exec 返回值**：原来 OCR 的「翻译」是 `self.accept()` 存
> `_translate_text`，`on_execute` 在 `dlg.exec()` 之后读它再 `invoke_plugin`。
> 改非模态后这行永远读不到 → 改成在按钮回调里**直接** `self._api.invoke_plugin("translator", text)`。

> **★ 第 2 条红线（2026-11-05）：常驻后台进程必须能关掉、且窗口开着时也能自动释放。**
> 本地大模型（llama-server）就绪就占 **数 GB 内存**，OCR / 翻译引擎也有几百 MB。
> 本地 AI 插件曾出现「打开后一直挂着 llama-server、用户没法关」——查下来是两层问题：
> ① 没有手动开关；② **空闲释放定时器只在 `closeEvent` 里调度**，窗口开着时永不触发。
>
> 凡启动常驻进程的插件，三件事缺一不可：
> 1. **手动启停开关**（顶栏按钮）：就绪显示「停止引擎」+ tooltip 带 `memory_mb()` 实际占用；
>    停止后显示「启动引擎」，点击重新拉起。用一个 `_sync_power()` 统一同步按钮文字、
>    状态点、发送/识别按钮的可用性。
> 2. **窗口开着时也生效的空闲自动释放**：不要只在关窗时调度，**每次任务完成**（打字机收尾 /
>    识别完成 / 翻译完成）都重新调度定时器；有新任务时取消定时器。
>    配置 `idle_release`（秒）要暴露在 `plugin.json` 的 `Config` 里，允许 `0` = 不自动释放。
> 3. **自动释放后能无感恢复**：区分「用户手动停」和「空闲自动停」——
>    手动停就明确提示、不自动拉起；自动停则把新请求**排队**，后台重新拉起引擎，
>    `ready` 后自动把排队的请求发出去（用户不该感知到引擎被停过）。
>    ⚠️ 手动停止时拦截发送要**在消息入历史之前**，否则会出现「有气泡没回答」。
>
> 参考实现：`plugin_pack/llm_chat/main.py` 的 `_sync_power` / `_toggle_engine` /
> `_on_idle_stopped` / `_ensure_engine` / `_pending_images`；
> `plugin_pack/doc_recognize/main.py` 的 `power_btn`（手动启停）；
> `plugins/translator/main.py` 的空闲释放缓存。
> **判据**：窗口开着 N 分钟不用，后台进程还在占内存 → 就是 bug。

> **★ 第 3 条红线（2026-11-05）：`deleteLater()` 之后，别再碰那个控件。**
> PySide6（Qt6）控件是「Python 包装对象 + 底层 C++ 对象」两层。销毁后 Python 变量还在，
> 再调 `w.xxx()` 就抛 `RuntimeError: wrapped C/C++ object of type Xxx has been deleted`。
> Qt 会接住异常**不崩**，但控制台刷屏、且回调里的收尾（清透明效果、显示用量/复制按钮）没跑完。
> 上一轮把 `setParent(None)` 改成 `deleteLater()` 修「游离顶层窗口」时，正是这个改动让
> 所有异步回调踩空（之前控件只是脱离布局、没真销毁，泄漏但不报错）。
>
> 三条做法：
> 1. **定时器用 `QTimer(widget)`**（parent = 控件），控件销毁时定时器作为子对象一起没，
>    回调根本不触发；**别用** `QTimer.singleShot(400, lambda: w.setGraphicsEffect(None))`
>    这种捕获裸 widget 的写法。动画 `finished` / 线程 `finished` 回调同理要兜底。
> 2. **`destroyed` 回调里只清引用**（`global _DLG; _DLG = None`），**绝不调 `_DLG.isVisible()`
>    等任何控件方法**——信号发出时 C++ 对象已经没了。
> 3. **销毁控件前先停异步源**：`typer.reset()` + 清 `_pending` / `_ai_block`，
>    再 `deleteLater()`；收尾回调里仍加 `try/except RuntimeError` 双保险。
>
> ⚠️ 别依赖 `sip`：0.36 的 PyQt6 要 `from PyQt6 import sip`；**0.37 起用 PySide6 / `shiboken6`**,
> 且 `sip.isdeleted()` 在 `deleteLater()` 后不一定及时返回 True —— 用 parent 生命周期
> + 异常兜底更稳。
> **判据**：切会话 / 清空 / 关窗口时控制台有没有 `wrapped C/C++ object ... has been deleted`。

> **★ 第 4 条红线（2026-11-05）：插件入口 `main.py` 里不要用 `@dataclass`。**
> 主程序用 `importlib.util.spec_from_file_location(...)` + `exec_module()` 加载插件，
> **不会把模块注册进 `sys.modules`**。而 Python 3.14 的 `dataclasses` 解析字段类型时
> 要去查 `sys.modules[cls.__module__].__dict__` —— 查不到就是 `None` →
> `AttributeError: 'NoneType' object has no attribute '__dict__'`，
> **插件在「加载阶段」就炸**，用户看到的现象是「插件打不开」（运行阶段完全没进去）。
>
> 真实事故：文档识别插件做队列功能时给入口加了个 `@dataclass QueueItem`，
> 结果插件整个打不开。排查绕了一圈才定位到是**加载阶段**而不是运行阶段的问题。
>
> 两条正确做法：
> 1. **插件入口里的类用普通类**（`__init__` + `@property`）——改插件即可立刻生效，
>    不用等重打主包。`dataclass` 只能放在**被普通 import** 的模块里
>    （如 `doc_engine.ModelSpec` 就没问题，因为 `import doc_engine` 会自动注册 sys.modules）。
> 2. **主程序侧根治**：`liytools/plugins/manager.py` 加一行
>    `sys.modules[spec.name] = module`（importlib 的标准做法），失败时
>    `sys.modules.pop(...)` 清掉半成品。这条要重打主包才生效。
>
> ⚠️ 顺带注意：所有插件目录都会被 `sys.path.insert(0, ...)` 插到最前面，
> 所以**不同插件的模块不能重名**（后插入的目录遮蔽前面的）。现有模块名
> `doc_engine` / `md_render` / `inputs` / `chat_engine` / `md_chat` / `session_store`
> 均不重名；各插件入口都叫 `main.py` 没关系（主程序按**绝对路径**加载，不靠 import）。
> **判据**：插件"打不开"、插件管理页只显示一条 `AttributeError` 时，
> 先怀疑**入口模块顶层的装饰器**，而不是插件自己的业务逻辑。

1. **弹窗样式**：直接套统一主题——`from liytools.plugins import theme` 然后
   `dialog.setStyleSheet(theme.DIALOG_QSS)`。主按钮设 `setProperty("role", "primary")`
   变蓝色（如「翻译/识别/保存」）；次按钮（设置/取消）不设即浅灰。文案提示用
   `setWordWrap(True)` 且给浅灰小字。
2. **后台任务防卡 UI**：耗时的（翻译/OCR/网络请求）放 `QThread` 子类 + `pyqtSignal`
   回传结果，**不要**在 `on_execute` 里直接同步跑。参考 `plugins/translator/main.py`
   的 `TranslateWorker` / `plugins/ocr/main.py` 的 `OcrWorker`。
3. **跨插件调用**：OCR 识别完把文本交给翻译 → `api.invoke_plugin("translator", text)`；
   截图完成把图交给 OCR → `api.invoke_plugin("ocr")`（OCR 打开会自动读剪贴板图）。
4. **剪贴板图片**：截图插件写图片用 `api.clipboard_write_image(img)`；OCR 读剪贴板图
   直接走 Qt `QApplication.clipboard().image()`（PluginAPI 只开放文本）。
5. **弹窗里用 QDialog**：需要 `accept()`/`reject()`/`finished` 信号的窗口必须继承
   `QDialog`，继承 `QWidget` 会没有这些方法（截图插件曾因此卡死）。
   ⚠️ 但「继承 QDialog」≠「可以 `exec()`」——插件主窗口按本章头号红线走 `show()`；
   `exec()` 只留给短时确认框。
6. **窗口置顶**：需要"系统级最上、不被其它窗口盖"的小窗口（如截图工具栏）用
   `setParent(None)` + `setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)`，交互结束后
   `raise_()` 再顶一次（全屏遮罩激活会把它盖住）。
