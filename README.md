# BiliMusicCLI

命令行 + 语音的 B 站音乐助手：说一句话或敲一条命令，自动完成「搜索 → 下载音频 → 播放 → 缓存」。

- 语音点歌：唤醒词「小爱同学」或「播放」，说出歌名即可播放
- 精准检索：走 B 站官方搜索接口，按时长 / 分区 / 标题相似度综合排序，过滤「盘点类」无关视频
- 多线程下载：分片 + 断点续传，保存为 `.m4s`，可批量转 MP3
- 本地缓存：SQLite 记录播放过的歌，重复播放秒开，播完自动切下一首
- micro:bit 遥控：暂停 / 切歌 / 音量 / 状态回传 / 进度显示（见下方「串口协议」）
- 中英文双语界面

---

## 快速开始

```bash
# 1. Python 3.10+
pip install -r requirements.txt

# 2. 准备两个二进制（体积大，不入库，需自己放或使用下面的打包）
#    bin/libmpv-2.dll   —— 播放用的 mpv 运行库
#    ffmpeg.exe         —— 格式转换
#    自己打包/还原：
#        python make_runtime_pack.py           # 生成 runtime_library.7z（约 60 MB）
#        7z x runtime_library.7z -o<项目目录>   # 别人拿到后解压即可

# 3. 启动
python main_CUI.py

# 4.（可选）扫码登录，登录后才能拿到 320kbps / Hi-Res 音质
#    在控制台输入 login，扫二维码即可；也可以首次初始化时就登录
```

首次启动会进入初始化向导，配置语言、目录、下载线程数、麦克风和 micro:bit。

---

## 命令一览

也可以直接把 B 站链接粘进命令行，`download` 会自动识别。

| 命令 | 作用 |
| --- | --- |
| `help [命令]` | 查看全部命令或某个命令的详细用法 |
| `search <歌名> [歌手]` | 手动搜索，列出候选让你挑 |
| `download <链接...>` | 按链接下载音频（可多个） |
| `voice` | 进入语音点歌模式（Ctrl+C 退出） |
| `pause` | 暂停 / 继续 |
| `cache list \| clean [BV号]` | 查看或清理缓存 |
| `transform` | 选一个文件转成 MP3 |
| `batch_transform [目录]` | 批量把目录里的音视频转成 MP3 |
| `batch_search` | 读 txt（歌名或链接），批量下载 .m4s |
| `batch_extract [文件]` | 读 txt，批量下载并转成 MP3（自动删 .m4s） |
| `login status` \| `login` | 查看登录状态 \| 扫码登录 B 站 |
| `exit` | 退出 |

语音模式下可直接说：「暂停」「继续」「下一首」「换个版本」「停止」。

---

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `microbit/` | micro:bit 相关的一整套：桥接模块、板端固件、校验与真机联调脚本 |
| `review/` | 评测与人工复核类脚本（搜索评测集、期望 BV 号确认），**暂搁置**，原因见「搜索反馈埋点」 |
| `main_CUI.py` | 主控：命令注册、主循环、各子流程 |
| `song_search.py` | 搜索与排序（官方接口优先，网页解析降级） |
| `download_audio.py` | 取音频流（WBI 签名 → playurl）并下载 |
| `downloading.py` | 多线程分片下载、断点续传、进度条 |
| `generate_params.py` | B 站 WBI 签名（动态取密钥） |
| `cache_manager.py` | SQLite 歌曲缓存 |
| `player.py` | mpv 播放封装，播完自动切下一首；含音量与进度读取 |
| `microbit/microbit_bridge.py` | micro:bit 串口桥接：协议编解码 + 回调分发 |
| `microbit/microbit_remote.py` | **跑在开发板上**的 MicroPython 参考实现（不是给电脑 Python 用的） |
| `microbit/stub_microbit.py` | 上面那个固件的模拟层：假 display / 按键 / 串口 + 虚拟时钟 |
| `microbit/check_remote.py` | 用模拟层离线跑一遍固件，刷板前先验证（无需开发板） |
| `microbit/diagnose.py` | 板子「不收消息 / 没反应」时的一键链路诊断：找板子 → **查上次刷写有没有失败** → 原样监听 → REPL 探测 → 下发测试，最后直接给结论 |
| `microbit/hex_sync.py` | 校验 / 重新打包固件 hex，防止 `.py` 改了而 hex 没跟上 |
| `VoiceRecognition.py` | Silero VAD + FunASR 唤醒词与指令识别 |
| `transform.py` | 调用 ffmpeg 做格式转换 |
| `download_video.py` | 视频 DASH 下载与合并（独立脚本） |
| `title_cleaner.py` | 分层歌名清洗（去标签 / 歌词片段 / 噪声词 / 切段） |
| `main_CUI.py` 内的 `BiliLogin` | 扫码登录（`login` 命令），凭据存 `bilibili_cookies.json` |
| ~~`get_cookies.py`~~ | 曾经是独立的扫码登录脚本，功能已并入 `main_CUI.py` 的 `BiliLogin`，文件已删除（Git 历史里还能翻到） |
| `initial_setup.py` | 首次运行配置向导 |
| `lang.py` / `config.py` / `utils.py` / `select_file.py` | 语言包 / 请求头 / 工具函数 / 文件选择对话框 |
| `smoke_test.py` | 冒烟测试：一次性实跑全部可自动验证的功能 |
| `clean_title_verify.py` | 歌名清洗规则的独立验证用例 |
| `feedback.py` | 搜索反馈埋点：把每次搜索里「你选了第几个 / 翻页 / 取消 / 喊换个版本」记进本地 `feedback.db`，`python feedback.py` 出统计（`--selftest` 离线自测） |
| `review/eval_search.py` | 搜索排序评测：`python review/eval_search.py` 打分，`--audit` 跑出结果供人工核对（**暂搁置**，原因见「搜索反馈埋点」） |
| `review/eval_confirm.py` | 半自动填期望 BV 号（**暂搁置**，同上） |
| `microbit/microbit_verify.py` | 用虚拟串口验证 micro:bit 协议与串口链路，不需要硬件 |
| `microbit/microbit_live_test.py` | micro:bit 真机联调：连上串口后逐项下发报文、引导按键，双向验证（**需要开发板**）。结果分四类：通过 / 失败 / 跳过 / **硬件**（板子本身没反应，如 logo 失灵） |
| `microbit/key_capture.py` | 按键真机收集器：排好时间表自己收，不用人在电脑前一项项回车（**需要开发板**）。`--check N` 是开机体检（看新固件有没有启动就崩），`--plan` 一口气收完 9 项按键 |
| `progress_bar.py` | 播放进度条：纯渲染 + 后台刷新线程（生产代码） |
| `progress_bar_verify.py` | 播放进度条验证：`python progress_bar_verify.py [--file 歌曲] [--demo]` |

---

## 搜索是怎么排序的

数据源优先用官方接口 `/x/web-interface/search/type`（返回 bvid、时长、分区等结构化字段），接口不可用时才降级为搜索页 HTML 解析。**标题与 BV 号严格一一对应**，不会出现串歌。

硬性淘汰：

- 标题命中黑名单：纯享、循环、盘点、十大、合集、科普、教学、reaction……
- 分区明显与音乐无关：游戏、科普、数码、影视、鬼畜……
- 时长超过 10 分钟

标题会先过一遍**分层清洗**（见 `title_cleaner.py`）：去掉 `【】[]` 标签、引号包裹的歌词片段、书名号保留歌名、剔除录音棚/试听/动态歌词/无损/Hi-Res/4K/60FPS 等噪声词，再按 `|` 与 `·` 切段过滤。清洗结果同时用作文件名，所以落地的 `.m4s` 不会带《》「」『』之类的杂符号。

相似性比较用的是**清洗后的最佳匹配片段**，不是整条标题 —— 搜「约会 音乐」时，标题 `RADWIMPS - デート - 约会` 里的「约会」片段能打满分，所以不会被「约会大作战背景音乐」压过去。

打分（越高越靠前）：

| 项 | 权重 |
| --- | --- |
| 标题与查询词的相似度 | ×100（含完整匹配额外加权） |
| 音乐类分区（MV / 音乐综合 / 翻唱…） | +25 |
| 时长落在 1.5–7 分钟 | +15 |
| 高音质关键词（Hi-Res / 无损 / 母带…） | 每个 +6，**重叠部分只算一次** |
| 播放量（收益递减） | 最多 +10 |
| 平台原生排序（totalrank 名次） | 第 1 名 +12，每降一名 -2，第 7 名起为 0 |

两个容易踩坑的地方，实现上都做了处理：

- **关键词不能重复计分。** 词典里存在包含关系的词（如 `无损` / `无损音质`），若逐个做 `word in title`，标题里一处 `Hi-Res无损音质` 会被算成三四个关键词，堆砌音质词的标题就能刷出虚高分。现在按词长降序匹配，命中区间标记为已消费，重叠部分不再重复计入。
- **关键词匹配要折叠大小写与花式写法。** NFKC 归一化 + 转小写后，`Hi-res`、`HI-RES`、全角 `Ｒｅｍａｓｔｅｒ`，甚至标题里常见的花式粗斜体 `𝐇𝐢-𝐑𝐞𝐬`，都能正常命中词典。

参数都集中在 `song_search.py` 的类属性里，方便调参做对比实验。改动任何一项后建议跑一次 `python review/eval_search.py` 确认没有回归。

---

## micro:bit 串口协议

115200 波特率，每行一条报文，全部 ASCII。板端参考实现在 `microbit/microbit_remote.py`。

主机 → 板子：

| 报文 | 含义 |
| --- | --- |
| `STATUS:<play\|pause\|stop>` | 播放状态 |
| `VOL:<0-100>` | 音量 |
| `TITLE:<text>` | 当前歌名（已做 ASCII 化） |
| `TIME:<pos>/<dur>` | 播放进度（秒），未知时为 `?` |
| `STATE:…` | 回应 `QUERY`，一次把上面全发出去；每 5 秒也会主动同步一次 |

板子 → 主机：

| 报文 | 含义 |
| --- | --- |
| `PAUSE` | 播放 / 暂停切换 |
| `NEXT` / `PREV` | 下一首 / 上一首 |
| `STOP` | 停止 |
| `VOLUP` / `VOLDOWN` / `VOL:<n>` | 音量 ±10 / 直接设值 |
| `QUERY` | 请求全量状态 |

### 板端按键

`microbit/microbit_remote.py` 里写死了这套映射，V1 / V2 完全一致：

| 操作 | 发出的指令 | V2 | V1 |
| --- | --- | --- | --- |
| A 短按 | `PREV` 上一首 | ✅ | ✅ |
| A 长按（>0.6s） | `VOLDOWN` 音量 −10 | ✅ | ✅ |
| B 短按 | `NEXT` 下一首 | ✅ | ✅ |
| B 长按（>0.6s） | `VOLUP` 音量 +10 | ✅ | ✅ |
| A+B 短按 | `PAUSE` 播放 / 暂停 | ✅ | ✅ |
| A+B 长按（>0.6s） | `STOP` 停止 | ✅ | ✅ |
| 触摸 logo | `PAUSE` 播放 / 暂停（和 A+B 短按等价） | ✅ | — |
| 摇一摇 | `QUERY` 重新拉取状态 | ✅ | ✅ |

> 以前是「V2 组合键发 STOP、V1 发 PAUSE」，等于把「暂停」整个押在 logo 触摸上。
> logo 就是正面那块金色触摸区，氧化或沾汗后容易失灵，一失灵就再也暂停不了。
> 所以改成 A+B 短按也能暂停，logo 只当作多出来的一个快捷键。

屏幕这边有两条刻意的取舍：

- **音量不挂在歌名滚动串的尾巴上**。三十几个字符滚过去，末尾的 `V65` 一闪就没了，
  根本看不清。音量改成变化时**静态闪一下**「V」再逐位闪数字（`GLYPH_BRIGHT` 控制亮度）。
- 自定义字模和图标用低亮度（默认 4，满亮是 9）。`display.scroll()` 没法调暗
  （MicroPython 没暴露亮度接口），所以滚动文字仍然是满亮 —— 嫌刺眼只能物理贴层磨砂胶带。

想改就编辑固件顶部的常量（`LONG_PRESS_MS`、`COMBO_WINDOW_MS`、`GLYPH_BRIGHT` 等）。

### 播放进度条

`progress_bar.py` 每 0.5 秒原地重画一行 `00:37 [██████░░░░░░] 04:22`。三档开关策略：

| 场景 | 默认 | 原因 |
| --- | --- | --- |
| 命令模式 | **关** | 后台刷新会把你 `input()` 里正在敲的内容顶掉。想看就敲 `progress`（`progress force` 可强行开） |
| 语音模式 | 开 | 没人用键盘输入，这个代价不成立 |
| 连着 micro:bit | 关 | 进度已经由 bridge 的 keepalive 推到点阵屏上了，两边同时刷只会互相干扰 |

`python progress_bar_verify.py --demo` 可以现场看「刷新 vs 输入」的冲突有多难看。

### 刷之前先验一遍

```bash
python microbit/check_remote.py     # 不需要开发板
```

### 把固件打包成 .hex

两种办法，挑顺手的：

1. **官方在线编辑器**（推荐，runtime 版本最稳）：打开 <https://python.microbit.org>，
   把 `microbit/microbit_remote.py` 整个粘进去，Download 出的 `.hex` 拖进 MICROBIT 盘。
2. **直接用仓库里打好的** `microbit/microbit_remote.hex` —— 拖进 MICROBIT 盘即可。

⚠️ 第 2 条有个前提要清楚：本地 `uflash` 内置的 MicroPython runtime 是
**2.0.0-beta.5**（2021 年的老版本）。V2.0 / V2.1 板子没问题，但
**V2.2 板子请用第 1 种办法**（官方编辑器用的是当前正式版 runtime）。
换句话说，`.py` 才是唯一可靠的真源，hex 只是图方便的产物。

```bash
# 改了固件源码后，重新打包（需要 pip install uflash）
python microbit/hex_sync.py --regen

# 只校验 hex 有没有落后于源码
python microbit/hex_sync.py
```

hex 是二进制，git diff 看不出内容，改了 `.py` 忘了重新打包的话没有任何人会注意到。
所以打包时会顺带写一个指纹文件 `microbit_remote.hex.sha256` 记录当时源码的 sha256，
`python microbit/check_remote.py` 每次都会校验它（第 [12] 组）。

它会在模拟出来的 display / 按键 / 串口上真跑一遍固件，覆盖 import 是否成立、
按键状态机（短按 / 长按 / 组合 / 边沿触发）、报文解析、屏幕有没有被刷爆，
还会核对固件发出的每条指令主机都认得。目前 49 项。

**坑预警**：

- micro:bit 自带的 5×5 点阵字体只有 ASCII，中文歌名直接推过去是一串方块。
  `TITLE:` 会先做 ASCII 化 —— `pypinyin` 把汉字转成拼音并**按字分开大写**
  （"晴天" → `Qing Tian`，拼成一坨 `qingtian` 在 5×5 点阵上没法读）；
  没装就退化成丢掉非 ASCII 字符，歌名直接显示成一个横杠。已在 `requirements.txt` 里列出。
- **固件收到内容没变的报文不要重绘。** 桥每 5 秒会把同样的 `STATUS`/`VOL`/`TITLE`
  重发一遍做心跳（keepalive），如果照单全收当变化处理，滚动到第 5 秒就会被掐回开头，
  屏幕看起来像在抽风。同理，周期性重滚的间隔不能写死成 6 秒 —— 三十几个字符的标题
  滚完要半分钟，固定周期永远够不上，只能看到开头那一小段。现在间隔是按滚动串长度估的。
- 固件一跑就让 REPL 失效（`uart.init()` 占用了同一个串口），
  所以刷进去之后 Mu / Thonny 的串口监视器就连不上了 —— 这是正常的，
  想改程序直接用编辑器重新刷一次 `.hex`，不必先恢复 REPL。

---

## 已知限制

- 语音模型首次加载约 1–2 分钟（SenseVoiceSmall + Silero VAD）
- 未登录时音质上限约 192kbps；扫码登录后才有 320kbps / Hi-Res
- 语音模式下命令行暂时不可用，按 Ctrl+C 退出语音
- WBI 密钥由接口动态获取，网络异常时会回退到内置兜底值，可能失效
- `bilibili_cookies.json` 内含登录凭据，**不要提交到仓库**（已在 `.gitignore` 中忽略）

---

## 测试

```bash
python clean_title_verify.py            # 只验证歌名清洗规则
python review/eval_search.py            # 搜索排序评测（3 条用例，含「雨爱」「花海」）
python smoke_test.py                    # 全量冒烟测试（含下载链路，会联网）
python microbit/check_remote.py         # micro:bit 板端固件（模拟运行，无需开发板）
python microbit/microbit_verify.py      # micro:bit 主机侧协议（loop:// 虚拟串口，无需开发板）
python microbit/microbit_live_test.py   # micro:bit 真机联调（需要插着板子，交互式）
python microbit/diagnose.py             # 板子没反应时的一键链路诊断（会直接给结论）
python progress_bar_verify.py           # 播放进度条；--file 指定歌曲，--demo 看刷新干扰
```

`microbit/microbit_verify.py` 用 pyserial 自带的 `loop://` 虚拟串口，所以不插开发板也能跑通主机侧逻辑。

真按键、真屏幕要靠 `microbit/microbit_live_test.py`（先把 `microbit/microbit_remote.py` 刷进板子）：

```bash
python microbit/microbit_live_test.py            # 五组全跑：连接 / 主机下发 / 按键手势 / 协议边界 / 拔线重连
python microbit/microbit_live_test.py --monitor  # 只盯着串口看板子发来什么
python microbit/microbit_live_test.py --play     # 手动模式，自己敲命令下发报文
python microbit/microbit_live_test.py --auto     # 无人值守：跳过一切需要人手的部分
```

它会逐项打印「现在请按什么、屏幕上应该出现什么」，按键类会实时等待板子的回应。
注意串口一次只能被一个程序占用，跑之前把 Mu / Thonny / 串口助手都关掉。

按键那九项如果不想守着电脑敲回车，用 `microbit/key_capture.py` —— 它按时间表自己收：

```bash
python microbit/key_capture.py --check 8        # 开机体检：刚刷完固件看有没有 PANIC 上报
python microbit/key_capture.py --plan           # 一口气收完 9 项按键（约 110 秒）
python microbit/key_capture.py --plan --only 3  # 只重测第 3 项
```

提示会同步写进 `microbit/temp/NOW.txt`（当前该按什么）和 `microbit/temp/PLAN.txt`
（带钟点的完整时间表），开着其中一个跟着按就行；结果出在
`microbit/temp/key_capture_result.md`。
新固件真机上崩了会往串口报 `PANIC:<异常类>:<信息>`，这两个脚本都会把它单独标出来。

板子突然「没反应 / 不收消息」时先跑 `python microbit/diagnose.py`：
它按层测一遍 —— USB 认不认得 → **上次刷写成功了吗** → 板子开不开口 →
是不是掉进 REPL → 下发报文看屏幕，最后直接给结论和下一步，省得一层层手动试。

刷固件的两条路里，**V2.2 建议用官方编辑器**（python.microbit.org 里新建项目、
把 `microbit/microbit_remote.py` 整份粘进去、下载 hex 拖到 MICROBIT 盘）。
`hex_sync.py --regen` 走的是 uflash 自带的 MicroPython 2.0.0-beta.5，
这个 runtime 在 V2.2 上偶尔会出现「COM 口认得、但数据一个字节都不通」。

⚠️ **刷写这一步本身也会失败，而且电脑上一声不吭** —— DAPLink 只在 MICROBIT 盘上
留一个 `FAIL.TXT`。最常见那条 `File sent out of order by PC` 是文件块被写乱序了
（磁盘缓存 / 杀毒软件实时扫描 / U 盘写入策略都会触发），结果只刷进去半截固件，
表现为串口乱码、板子静默、屏幕哭脸 —— 全都像是「程序没跑」，特别容易误判成
代码 bug 或硬件坏了。实测最稳的写法是 **PowerShell 的 `Copy-Item`**
或资源管理器拖放（Git Bash 的 `cp` 会触发乱序）；刷完一定回来看一眼
`FAIL.TXT` 还在不在 —— **它在，就说明这一遍白刷了**。

## 搜索反馈埋点（现在在用的办法）

> **为什么人工评测集先搁置了**
>
> 建完 29 条用例后做了一次统计：26 条待确认里，**23 条的前 3 名全是同一首歌的不同版本**
> （录音棚版 / 4K 修复 MV / Hi-Res 版……）。程序怎么排都对，人怎么选也只能在这几个里选 ——
> 因为双方看的是同一批候选。这类用例测出来的永远是高分，信息量为零，却要花掉全部标注时间。
>
> 真正有价值的是「程序错了、人不同意」的那一下。那一什么时候发生，只有真的在用的时候才知道。
> 所以改成埋点，让日常使用替我们把错挑出来。

**不猜用户想要什么，用户的行为本身就是标签。** 而且全是他本来就有的动作，零额外操作：

| 记录的动作 | 触发时机 | 怎么解读 |
| --- | --- | --- |
| `accept` | 候选列表里回车、或输入 `1` | 认可推荐 → 正样本 |
| `pick` | 输入 `2`~`N` 选了别的 | 否定了 Top1 |
| `page` | 按 `r` 翻页 | 这一整页都不行 |
| `cancel` | 按 `q` 取消 | 压根没找到想要的 |
| `switch` | 语音喊「换个版本」 | 播了但不对，最强的负信号 |
| `auto` | 语音 / 批量直接播了 Top1 | 没接着喊换版本，就是默认认可 |

每条记录存：时间、关键词、候选列表（BV / 标题 / 时长 / 分数）、你选了第几个、来源（cli / voice / batch）。

```bash
python feedback.py            # 看统计
feedback                      # 程序里也一样看
feedback clear                # 清空重来
python feedback.py --selftest # 离线自测（临时库，不动真实数据）
```

**三条底线**：只记关键词和候选视频，不记账号、不上传，数据只在本地 `feedback.db`；
`config.ini` 里 `[feedback] enabled = 0` 能彻底关掉（关了以后一个字都不写）；
埋点代码自己兜住所有异常，宁可不记也不会影响播放。

**什么时候来分析**：攒够 **100 条**以上。100 条大概能把 Top1 接受率的误差压到 ±10%，
样本太少时看出来的「规律」基本是噪声 —— 刚开始攒数据的时候这条最容易踩。
到量之后重点看报告末尾那张表：**被否得最多的 Top1**，那就是排序最该改的地方。

---

`review/eval_search.py` 的用例格式是 `(查询词, 期望 BV 集合, 备注)`：同一首歌在 B 站往往有多个可接受的版本，所以期望值写成集合，命中任意一个即算通过。**新增用例前请务必核实 BV 号确实对应这首歌**，不要凭印象填。

正确的扩充流程是两步，别反着来：

```bash
python review/eval_search.py --audit   # 1. 先跑，把 Top5 写进 review/eval_audit.md
# 人工核对：哪些 BV 号确实是这首歌？填进 eval_search.py 的 CASES
python review/eval_search.py           # 2. 再打分
```

期望集合为空的用例会被打分模式跳过。千万不要拿程序跑出来的 Top1 反过来填期望值 ——
那样准确率必然是 100%，评测集就白建了。

一条条手查 BV 号太烦，用 `review/eval_confirm.py` 半自动做：

```bash
python review/eval_confirm.py               # 逐条：自动开 B 站搜索页 -> 你粘链接 -> 它洗 BV 号
python review/eval_confirm.py --start 10    # 接着上次的进度
python review/eval_confirm.py --rounds 5    # 一条最多粘几次（默认 3）
python review/eval_confirm.py --apply       # 确认完写回 eval_search.py（改前自动备份到项目根 backup/）
python review/eval_confirm.py --selftest    # 离线自测粘贴流程（不联网不弹浏览器）
```

粘什么都行 —— 完整网址、`b23.tv` 短链、纯 BV 号、`av` 号、B 站 App 分享的那一长串，
脚本都会洗出 BV 号，再调接口把标题查出来给你二次核对（贴错链接时标题一眼就能看出来）。
过程中 `q` 随时结束并保存，已确认的部分不会丢，下次接着跑。

**一条用例最多粘 3 次（`--rounds` 可调）**，多轮的结果合并去重。
之所以给多次：一首歌在 B 站往往有好几个像样的版本（官方 MV、专辑音源、现场、翻唱、
不同 UP 主转投），只锚定一个 BV 号会让分数虚低，反过来也容易碰巧命中。
所以每条收一组「可接受答案」，打分时 Top1 命中其中任意一个就算通过。
粘够了直接敲空行就能提前收工；一条都没粘的空行不占次数。

`smoke_test.py` 末尾会打印无法自动验证的项目（语音、micro:bit、GUI 对话框、扫码登录等），这些需要手动确认。

---

## 备份

`backup/` 目录存放改动前的原始文件快照，需要回滚时从对应日期目录里取（该目录同样不进版本库）。

## 许可

MIT，详见 [LICENSE](LICENSE)。
