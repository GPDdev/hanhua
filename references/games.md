# 游戏

prepare 读取安装目录并在独立的 .work 目录准备资源。Ren’Py 使用官方 SDK 生成 schinese 模板，已有本地翻译保留。若游戏与默认 SDK 的主版本不同，可设置 HANHUA_RENPY_SDK 指向兼容的 SDK 根目录或 renpy.exe，再用 prepare --refresh。RPG Maker MV/MZ 读取公开的数据库和事件 JSON，只翻译显示用字段。Godot 表格、gettext、Unity TextAsset 与 Unreal 松散 locres 根据资源实际结构选择读写器。Unity 二进制 TextAsset、未知归档和音频库需要专用适配。

补丁加载前按原始哈希核对文件。apply/restore 后自动复核写入结果，断电后可重试同一动作。补丁文件夹的 apply.cmd、restore.cmd 也可直接使用。相同安装目录下仅改动过的原件保存在 .hanhua-backup。换机器或移动游戏后传 --game 新目录。游戏更新后重新探测并生成匹配的补丁。

## 陌生格式

看到陌生引擎、未处理归档或仅识别部分内容时，先查游戏自带的模组说明和官方格式文档，必要时查该游戏的公开模组工具。对照文件头、版本和目录确认匹配。只在工作副本上试作，选取有代表性的字符串及一处变量测试。保留文档地址和格式版本至任务输出目录。没有资料或副本无法读回时，交付已经验证的部分并转入实时字幕。

单个自定义格式可在该任务输出目录编写独立 Python 适配器，调用 prepare <输入> --adapter <该文件> --refresh。适配器导出以下三个函数。

- scan(root) 返回条目列表。每个条目形如 {"path":"lang/messages.dat","entries":[{"loc":0,"source":"Open the door","speaker":"Guard","context":"城门"}]}，可另给 target 作为相对于游戏根目录的输出路径。
- render(source_copy,destination,replacements) 把原始副本与有序 (loc, text) 对写为新的资源文件。沿用原格式的索引、编码和边界。
- readback(path,loc) 使用格式读取器返回该条目。构建会先验证原文写回，再验证译文读回。还需用游戏或官方工具实际加载代表性文件，记录适用版本与未验证项。

公开模组入口要求特定目录、清单或语言设置时，按作品文档在隔离副本验证安装位置。游戏附带有效简中包时优先启动该语言。遇到字体缺字，优先采用作品的备用字体接口。引擎只有项目源文件、缺少发行版资源或未提供模组加载入口时，在报告中区分可编辑的资源与实际装入游戏的补丁。

## 实时字幕

用 live --list 列出窗口，live --pid <PID> 或 live --window <标题> 选择唯一游戏窗口。已知安装路径可以使用 live --game <目录> --wait 180，等待该游戏启动。持续显示翻译时以隐藏的后台 PowerShell 启动帮助程序，让玩家继续游玩。F10 切换字幕显隐，F12 退出。--seconds 可用于有界测试，--no-audio 用于仅识别画面，--out 指定存放事件记录的目录。记录包含实际捕获帧数、识别原文、译文和从捕获到翻译的耗时。

捕获使用 Windows.Graphics.Capture 和按进程的 WASAPI 回环。游戏不提供可捕获画面时先尝试其窗口化或无边框模式。独占显示、受保护的窗口和某些驱动的可捕获性依作品实测。实时 OCR 可通过 --lang ja、ko、en、ru、ar 或相应拉丁字母语言代码选择识字模型。游戏资源会用已提取的文字推断语言，影片使用音轨语言。HANHUA_OCR_LANGUAGE 可统一指定源语，HANHUA_OCR_MODEL 可指定兼容的 ONNX 模型。
