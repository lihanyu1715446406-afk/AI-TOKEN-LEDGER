# 本地 AI Token 账本

把你散落在各个 AI 编程工具里的 Token 用量,汇总成一张本机网页报表。

扫一遍 Codex、Claude Code、Cursor 在本机留下的日志文件,按天聚合出 Token 趋势、工具分布和活动时间分布,生成一个自包含的 `dashboard.html`。**全程不联网**,数据不出本机。

![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen) ![license](https://img.shields.io/badge/license-MIT-green)

## 支持的工具有哪些

| 工具 | 数据来源 | Token 统计 | 说明 |
|---|---|---|---|
| Codex | `~/.codex/sessions/**/*.jsonl` | ✅ | 由 `token_count` 事件的累计值按时间序列求增量 |
| Claude Code | `~/.claude/projects/**/*.jsonl` | ✅ | 取每条 assistant 消息 `usage` 字段,按单次请求值求和 |
| Cursor | `state.vscdb` 的 `composerHeaders` | ❌ | 本地库不含 Token 字段,只能统计会话数与时间分布 |

### 数据位于何处

Codex 和 Claude Code 三个平台路径一致,都在家目录下。Cursor 的 `globalStorage` 各平台不同,脚本会自动探测:

| 工具 | Windows | macOS | Linux |
|---|---|---|---|
| Codex | `~/.codex/sessions/` | 同左 | 同左 |
| Claude Code | `~/.claude/projects/` | 同左 | 同左 |
| Cursor | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | `~/.config/Cursor/User/globalStorage/state.vscdb` |

某个工具没装或没有记录时,脚本会跳过它,不影响其它工具的统计,不会报错。

## 环境要求

只有 Python,标准库,无需 `pip install`。Python 3.8 或更高版本,Windows / macOS / Linux 均可。

## 使用方法

三个平台通用的一条命令(在项目目录下执行):

```bash
python sync_tokens.py              # 输出 data/usage.json 和 dashboard.html
```

Windows 上如果 `python` 不在 PATH,用 `py -3 sync_tokens.py` 代替。跑完打开 `dashboard.html` 即可。

嫌敲命令麻烦的话,各平台都备了启动脚本,会自动找 Python、同步、再打开报表:

| 系统 | 启动方式 |
|---|---|
| Windows | 双击 `run.ps1`(推荐,提示为中文)或 `run.bat`(纯英文,CMD 兜底) |
| macOS / Linux | `chmod +x run.sh && ./run.sh` |

`run.ps1` 若被 PowerShell 执行策略拦下,改用:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

**只想先看看长什么样** — 不想让脚本读你的日志,就用脱敏示例数据生成演示页:

```bash
python sync_tokens.py --sample     # 输出 dashboard.sample.html
```

其它参数见 `python sync_tokens.py --help`。

每次运行都会全量重扫日志并覆盖上一次的输出,所以随时可以重跑。

> `run.sh` 在 macOS / Linux 下的实测尚未覆盖,如果遇到问题,直接用上面的 `python sync_tokens.py` 命令即可,效果完全一致。

## 注意事项

**关于隐私**

这是本项目最需要留意的地方:

- 脚本只读本机文件,不含任何网络请求。
- **采集**:时间戳、Token 计量字段、模型名、工作区目录名、会话 ID、工具与模式标记。
- **不采集**:对话正文、提示词、AI 回复、会话标题、API 密钥、完整文件路径、系统用户名(在家目录运行的会话会显示为「主目录」)。
- 但**工作区目录名本身就会暴露你在做什么** —— `dashboard.html` 上会列出 `my-thesis`、`job-hunting` 这类目录名。`data/usage.json` 和 `dashboard.html` 都是你的个人使用画像,已被 `.gitignore` 排除。**不要把它们提交到公开仓库。**

**关于统计口径**

- **缓存复用(Token)会算进总量。** Claude Code 的 `cache_read_input_tokens` 量级远大于输入输出,直接相加会让总量看着很大——这是缓存命中的量,不是新算的量。想看得更准,请分开看「输入/输出」和「缓存复用」两列。
- **AI 活跃时长 ≠ 你的工时。** 它是根据 Token 事件时间戳和活动片段推算的(间隔 ≤30 分钟合并,单个事件按 5 分钟计),只能反映"AI 在什么时候干活",不代表你本人坐在电脑前多久。
- **峰值并发**只表示记录单元的活动时段重叠,不是实时并发请求数。
- **数字与供应商账单不会完全一致。** Token 计数取决于各家日志字段的口径,重试、失败请求、服务端侧统计都可能对不上。这个工具适合看趋势和相对分布,不适合用来对账。
- Cursor 的 Token 一栏是空的,因为它的本地库里没有这个字段。

**关于稳定性**

解析的是各家工具**未公开的内部日志格式**,这些格式可能随工具更新而变化。如果某次升级后数据突然归零或报错,通常是字段改名了,改 `sync_tokens.py` 里对应的 `parse_*` 函数即可。

## 文件说明

```
sync_tokens.py            同步脚本(读取日志 + 生成报表,单文件无依赖)
run.ps1 / run.bat         Windows 启动器
run.sh                    macOS / Linux 启动器
data/usage.sample.json    脱敏示例数据,用于 --sample 演示
data/usage.json           你的真实数据(运行时生成,已 gitignore)
dashboard.html            生成的自包含报表(已 gitignore)
```

## 许可

MIT
