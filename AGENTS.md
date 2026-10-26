# talk-log

用这台 MacBook Air 的内置麦克风，录下工作日白天在电脑旁的对话，按对话切段，交百炼 qwen-audio 转写并区分说话人，每天出一份 Markdown。个人使用。

## 已停用（2026-10-26）

两个 launchd 任务已卸掉，本机数据目录已删，代码留着；云上的存储桶、RAM 子账号和百炼 API Key 没动。重新启用跑 `talklog install`。

- 停用原因：转写结果本来要交给云端 agent 分析，对话里难免有秘密，不想交给模型厂商（百炼 ASR 也算一家）。要继续得换本地 ASR，再在本机加一道敏感内容过滤，这道过滤目前做不好：
  - 有格式的（手机号、证件号、验证码）靠规则，人名、客户、项目靠自己维护的名单，这两类做得到；
  - 要看上下文才知道敏感的（“他下个月要走”、薪资、健康）只能靠模型理解语义，连人工标注对这类的一致率都只有一半左右（RedactionBench）；这台 M2 / 8GB 的 Air 只跑得动 4B 级别的本地模型，判不准。
- 恢复前要处理：
  - `raw/` 里存的百炼原始结果带 `file_url`，是 OSS 签名链接，里面有存储桶名和 AccessKey ID，保存前要去掉。
  - 停用前录的几段，每段约 2.9 分钟就报“音频断了约 20 秒（多半是睡眠）”切段，间隔很规律，不像真睡眠，原因没查。

## 做法（2026-10-26 定）

- **只区分说话人，不认人**：每场对话里标「说话人1、说话人2」，编号只在这一场里有效。声纹认人（叫出名字）先不做；以后要加，只能处理加了之后的录音和最近 7 天还留着的音频。
- **只录得到电脑旁边的**：合盖时麦克风硬件断开，录不到；去会议室没带电脑也录不到。录音时段里程序用 `caffeinate -i` 防闲置睡眠，合盖照样会睡。
- **固定用内置麦克风**（`MacBook Air麦克风`），不跟系统默认输入走：跟着默认走的话，连上 AirPods 时会去开耳机的麦，耳机就掉成通话音质。
- **按对话切段**，不按固定时长：连续 3 分钟没人声就结束这一段，单段最长 60 分钟（百炼说话人分离建议单段不超过 2 小时）。人声不到 3 秒的段丢掉。
- **转写**：`qwen-audio-3.1-asr-flash-filetrans`，`diarization_enabled: true`，结果每句带 `speaker_id`。只按人声时长计费，10-21 素材库评测实测约 0.05 元/小时音频。说话人分离只支持单声道。
- **数据都在本机**：音频经个人账号的 OSS 存储桶交给百炼，拿到结果马上删；桶上 `talklog/` 前缀 1 天过期兜底。原始音频本机留 7 天，转写记录一直留着。

## 账号和密钥

仓库是公开的，具体用的哪个账号、存储桶和 key 的名字记在本机不进仓库的 `~/.config/talk-log/NOTES.md`。

- 用个人的阿里云账号，不用公司账号：个人对话不走公司账号。
- OSS：北京的私有存储桶，生命周期规则让 `talklog/` 下超过 1 天的文件自动删；专用 RAM 子账号，策略只允许读写删 `<桶>/talklog/*`（`oss:PutObject`、`oss:GetObject`、`oss:DeleteObject`），往别处写、列文件都是 403。
- 百炼：一个专用的 API Key。
- 密钥文件 `~/.config/talk-log/env`（600，不进仓库，纳入 secrets-sync）：`DASHSCOPE_API_KEY`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_BUCKET`、`OSS_ENDPOINT`。
- 欠费时这一轮转写失败、下一轮再试，音频还在本机，充值后会补上（7 天内）。

## 运行

两个 launchd 任务（`talklog install` 装，`talklog uninstall` 卸）：

| 任务 | 做什么 | 日志 |
| --- | --- | --- |
| `com.thomas.talklog.recorder` | 常驻；录音时段（默认工作日 9:00–19:00）里录，时段外每 30 秒看一次钟 | `~/Library/Logs/talk-log/recorder.log` |
| `com.thomas.talklog.process` | 每 5 分钟一轮：转还没转的段、重写涉及那几天的 Markdown、删超过 7 天的音频 | `~/Library/Logs/talk-log/process.log` |

两个都是用户级 LaunchAgent（`~/Library/LaunchAgents/`，`RunAtLoad`），开机后用户登录了才启动，登录前不录；录音进程 `KeepAlive`，崩了会被拉起。系统设置 →「通用 → 登录项与扩展」里关掉它的「允许在后台」就不会再启动。

改了代码以后 `talklog install` 重装一次，录音进程才会用上新代码。

常用命令（在项目目录下 `uv run talklog …`，或直接 `.venv/bin/talklog …`）：

- `talklog status`：录音状态、今天录了几段、还有几段没转、两份日志的最后几行。
- `talklog today [日期]`：打印某天的记录。
- `talklog try <音频文件> [--out 结果.json]`：单个文件试转，打印分好说话人的结果（不进数据目录）。
- `talklog process`：手动跑一轮转写。

数据目录 `~/.local/share/talk-log/`：

- `audio/<日期>/<时分秒>.ogg` + 同名 `.json`（开始时间、时长、人声秒数）；正在录的那段是 `.ogg.part`。16k 单声道 Opus 24kbps，每小时约 11MB。
- `raw/<日期>/<时分秒>.json`：百炼原始结果加状态（`ok` / `no_speech` / `failed`）。失败重试 5 次后才写 `failed`，次数记在 `tries.json`。
- `transcripts/<日期>.md`：每天一份，由 raw 整份重写，可以随时重跑。
- `recorder.json`：录音进程的状态（`off_hours` / `listening` / `recording`）。

可选配置 `~/.config/talk-log/config.toml`（没写的用默认值，见 `talklog/config.py`）：

```toml
[record]
device = "MacBook Air麦克风"
days = [1, 2, 3, 4, 5]
start = "09:00"
end = "19:00"
silence_end_seconds = 180
max_segment_minutes = 60
min_speech_seconds = 3
[storage]
data_dir = "~/.local/share/talk-log"
keep_audio_days = 7
```

## 代码

- `talklog/recorder.py`：ffmpeg（avfoundation）出 16k PCM → webrtcvad 逐 30ms 判人声 → `Segmenter` 切段 → `OpusWriter` 编码。切段规则写在文件开头；`Segmenter` 是纯逻辑，测试直接喂帧。
- `talklog/transcribe.py`：OSS 上传、百炼提交和轮询、保存、重试、清理；`process()` 带文件锁，同一时间只跑一轮。
- `talklog/render.py`：raw → 每天的 Markdown，同一说话人连着的句子并成一行。
- `talklog/cli.py`：命令和 launchd plist。
- 测试：`mise run test`（或 `uv run pytest -q`），不碰网络和麦克风。

## 实测记录

- 2026-10-26：launchd 拉起的 ffmpeg 能拿到麦克风（第一次可能要在弹窗里允许）。麦克风刚打开时头几秒可能没数据，所以录音的时间基准从第一帧到手算起。
- 2026-10-26：系统语音合成的三人对话，Tingting 和 Meijia 两个女声被分成了同一个说话人，粤语的 Sinji 单独分出来。合成语音音色接近，不能代表真人；分离准不准以真实对话为准。
- 2026-10-26：工位旁 46 秒真实环境录音，分出 4 个说话人、11 句，717 token，不到 0.002 元。
- 系统里能合成中文的语音只有 Tingting、Meijia、Sinji，Eddy、Flo 等输出是空的。
