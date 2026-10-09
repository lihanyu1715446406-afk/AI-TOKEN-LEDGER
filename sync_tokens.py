#!/usr/bin/env python3
"""
本地 AI Token 账本 — 数据同步脚本

隐私边界（改代码前请先读这段）
--------------------------------------------------
采集：时间戳、Token 计量字段、模型名、工作区目录名、会话 ID、工具/模式标记。
不采集：对话正文、提示词、AI 回复、会话标题、API 密钥、完整文件路径、用户名。
行为：仅在本机读写文件；不含任何网络调用（无 requests/urllib/socket）。

输出 data/usage.json 与 dashboard.html 都只含上述结构信息，
但**仍然是你个人的使用画像**，默认已被 .gitignore 排除，请勿提交到公开仓库。
"""

import os
import sys
import json
import sqlite3
import datetime
from pathlib import Path
from collections import defaultdict

# 修复 Windows GBK 编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── 跨平台路径 ──────────────────────────────────────────
HOME = Path.home()
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DATA_FILE = DATA_DIR / "usage.json"
DASHBOARD_FILE = PROJECT_DIR / "dashboard.html"
SAMPLE_FILE = DATA_DIR / "usage.sample.json"
SAMPLE_DASHBOARD_FILE = PROJECT_DIR / "dashboard.sample.html"

# ── 工具数据源配置 ───────────────────────────────────────
# 只记录已知可靠的字段映射，不假设目录一定存在

def find_codex_sessions():
    """扫描 Codex sessions 目录，返回 JSONL 文件列表"""
    sessions_dir = HOME / ".codex" / "sessions"
    if not sessions_dir.exists():
        return []
    files = list(sessions_dir.glob("**/*.jsonl"))
    # 也检查 archived_sessions
    archived_dir = HOME / ".codex" / "archived_sessions"
    if archived_dir.exists():
        files.extend(archived_dir.glob("*.jsonl"))
    return sorted(files)


def find_claude_sessions():
    """扫描 Claude Code 项目对话文件"""
    projects_dir = HOME / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    files = []
    for f in projects_dir.glob("**/*.jsonl"):
        # 排除 telemetry 目录
        if "telemetry" in str(f):
            continue
        files.append(f)
    return sorted(files)


def find_cursor_db():
    """查找 Cursor AI 追踪数据库（~/.cursor 三个平台一致）"""
    db_path = HOME / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    if db_path.exists():
        return db_path
    return None


def find_cursor_state_db():
    """查找 Cursor 的 state.vscdb，逐个平台枚举候选位置。

    Cursor 的 globalStorage 目录在各平台不一致，不能写死 Windows 路径。
    """
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming")
        candidates.append(Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    elif sys.platform == "darwin":
        candidates.append(HOME / "Library" / "Application Support" / "Cursor"
                          / "User" / "globalStorage" / "state.vscdb")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")
        candidates.append(Path(xdg) / "Cursor" / "User" / "globalStorage" / "state.vscdb")

    # 兜底：Cursor 偶尔也会把数据放在 ~/.cursor 下
    candidates.append(HOME / ".cursor" / "User" / "globalStorage" / "state.vscdb")

    for c in candidates:
        if c.exists():
            return c
    return None


def safe_truncate(s, max_len=80):
    """安全截断字符串"""
    if not s:
        return ""
    s = str(s)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def extract_workspace_name(cwd):
    """从工作目录提取安全的项目名（仅目录名，不含完整路径）。

    家目录会被归一为「主目录」：否则在家目录里跑的会话会让目录名
    恰好等于系统用户名，直接暴露到仪表盘上。
    """
    if not cwd:
        return "未知"
    try:
        p = Path(cwd)
        try:
            if p.resolve() == HOME.resolve():
                return "主目录"
        except (OSError, RuntimeError):
            pass
        name = p.name
        if not name:
            name = p.parent.name or "根目录"
        # 兜底：目录名恰好是用户名时同样匿名化
        if name == HOME.name:
            return "主目录"
        return name
    except Exception:
        return "未知"


def parse_codex_sessions():
    """
    解析 Codex sessions JSONL。
    返回: list of daily records + per-session details
    """
    files = find_codex_sessions()
    if not files:
        return []

    all_token_events = []  # (timestamp, session_id, total_usage, last_usage, cwd, model)
    session_meta = {}       # session_id -> {cwd, origin, model, turn_count, labels}

    for fp in files:
        session_id = None
        cwd = None
        origin = None
        models = set()
        turns = 0
        has_tool_calls = False
        has_mcp = False

        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as fh:
                lines = fh.readlines()
        except Exception:
            continue

        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            t = obj.get("type", "")
            p = obj.get("payload", {}) if isinstance(obj.get("payload"), dict) else {}
            ts = obj.get("timestamp", "")

            if t == "session_meta":
                session_id = p.get("id", fp.stem)
                cwd = p.get("cwd", "")
                origin = p.get("origin", "")

            if t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                total_u = info.get("total_token_usage") or {}
                last_u = info.get("last_token_usage") or {}
                all_token_events.append({
                    "ts": ts,
                    "session_id": session_id or fp.stem,
                    "total_usage": total_u,
                    "last_usage": last_u,
                    "cwd": cwd or "",
                })

            # 收集模型信息
            if t == "turn_context" and p.get("model"):
                models.add(p["model"])

            # 统计 turn
            if t == "event_msg":
                pt = p.get("type", "")
                if pt == "task_started":
                    turns += 1
                if pt == "tool_call":
                    has_tool_calls = True
                if pt == "mcp_call" or (p.get("tool_name") and "mcp" in str(p.get("tool_name", "")).lower()):
                    has_mcp = True
            elif t == "response_item":
                # response_item 是与 event_msg 平级的顶层类型，不能嵌在上面的分支里
                if p.get("type") == "function_call":
                    has_tool_calls = True

        # 保存 session meta
        sid = session_id or fp.stem
        # 构建行为标签
        labels = []
        if origin:
            labels.append(f"来源:{origin}")
        if turns > 0:
            labels.append(f"{turns}轮对话")
        if has_tool_calls:
            labels.append("工具调用")
        if has_mcp:
            labels.append("MCP")

        session_meta[sid] = {
            "cwd": cwd or "",
            "workspace": extract_workspace_name(cwd),
            "origin": origin or "",
            "models": list(models),
            "labels": labels,
            "file": str(fp),
        }

    # 按 session_id 和时间排序 token events，计算增量
    # 先按 session_id 分组
    session_events = defaultdict(list)
    for ev in all_token_events:
        session_events[ev["session_id"]].append(ev)

    daily_data = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "record_units": 0,
        "sessions": [],
    })

    all_activity_events = []  # (timestamp, session_id, type)

    # Token 字段名映射（兼容不同版本）
    def tok(d, key, default=0):
        """安全获取 token 值，兼容多种键名"""
        if not isinstance(d, dict):
            return default
        return d.get(key, default)

    for sid, events in session_events.items():
        # 按时间排序
        events.sort(key=lambda e: e["ts"])

        # 计算增量（因为 total_token_usage 是累计值）
        prev_total = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }

        session_start_ts = None
        session_end_ts = None
        session_daily_input = 0
        session_daily_output = 0
        session_daily_cache = 0
        session_daily_reasoning = 0
        session_daily_total = 0

        for ev in events:
            cur = ev["total_usage"]
            if not isinstance(cur, dict):
                continue
            # 计算本次增量
            delta_input = max(0, tok(cur, "input_tokens") - prev_total["input_tokens"])
            delta_cached = max(0, tok(cur, "cached_input_tokens") - prev_total["cached_input_tokens"])
            delta_output = max(0, tok(cur, "output_tokens") - prev_total["output_tokens"])
            delta_reasoning = max(0, tok(cur, "reasoning_output_tokens") - prev_total["reasoning_output_tokens"])
            delta_total = max(0, tok(cur, "total_tokens") - prev_total["total_tokens"])

            # 如果增量全为0但total>0，说明只有一个快照（外部导入会话），直接用total
            if delta_total == 0 and cur.get("total_tokens", 0) > 0:
                delta_input = cur.get("input_tokens", 0)
                delta_cached = cur.get("cached_input_tokens", 0)
                delta_output = cur.get("output_tokens", 0)
                delta_reasoning = cur.get("reasoning_output_tokens", 0)
                delta_total = cur.get("total_tokens", 0)

            # 提取日期
            try:
                if ev["ts"]:
                    dt = datetime.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                    date_str = dt.strftime("%Y-%m-%d")
                else:
                    continue
            except Exception:
                continue

            if session_start_ts is None or ev["ts"] < session_start_ts:
                session_start_ts = ev["ts"]
            if session_end_ts is None or ev["ts"] > session_end_ts:
                session_end_ts = ev["ts"]

            session_daily_input += delta_input
            session_daily_output += delta_output
            session_daily_cache += delta_cached
            session_daily_reasoning += delta_reasoning
            session_daily_total += delta_total

            # 累计到日期
            daily_data[date_str]["input_tokens"] += delta_input
            daily_data[date_str]["output_tokens"] += delta_output
            daily_data[date_str]["cache_tokens"] += delta_cached
            daily_data[date_str]["reasoning_tokens"] += delta_reasoning
            daily_data[date_str]["total_tokens"] += delta_total

            # 活动事件
            all_activity_events.append({
                "ts": ev["ts"],
                "session_id": sid,
                "date": date_str,
                "tool": "codex",
            })

            prev_total = {
                "input_tokens": tok(cur, "input_tokens"),
                "cached_input_tokens": tok(cur, "cached_input_tokens"),
                "output_tokens": tok(cur, "output_tokens"),
                "reasoning_output_tokens": tok(cur, "reasoning_output_tokens"),
                "total_tokens": tok(cur, "total_tokens"),
            }

        # Session 级信息
        meta = session_meta.get(sid, {})
        models = meta.get("models", [])

        # 确定日期（用 session 开始时间）
        try:
            if session_start_ts:
                dt = datetime.datetime.fromisoformat(session_start_ts.replace("Z", "+00:00"))
                session_date = dt.strftime("%Y-%m-%d")
            else:
                session_date = "unknown"
        except Exception:
            session_date = "unknown"

        session_info = {
            "id": sid[:30] + ("..." if len(sid) > 30 else ""),
            "workspace": meta.get("workspace", "未知"),
            "models": models,
            "labels": meta.get("labels", []),
            "input_tokens": session_daily_input,
            "output_tokens": session_daily_output,
            "cache_tokens": session_daily_cache,
            "reasoning_tokens": session_daily_reasoning,
            "total_tokens": session_daily_total,
            "start_ts": session_start_ts,
            "end_ts": session_end_ts,
        }

        if session_date in daily_data:
            daily_data[session_date]["sessions"].append(session_info)
            daily_data[session_date]["record_units"] += 1

    return dict(daily_data), all_activity_events


def parse_claude_activity():
    """
    解析 Claude Code 项目 JSONL，提取 Token 用量和活动时间。
    Token 来自每条 assistant message 中的 usage 字段（单次请求值，直接求和）。
    不读取对话正文，只提取结构信息和用量数字。
    """
    files = find_claude_sessions()
    if not files:
        return [], []

    all_activity = []
    daily_data = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "record_units": 0,
        "sessions": [],
    })

    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as fh:
                lines = fh.readlines()
        except Exception:
            continue

        if not lines:
            continue

        session_id = None
        mode = None
        cwd = None
        first_ts = None
        last_ts = None
        is_subagent = "subagents" in str(fp).lower() or "subagent" in str(fp).lower()
        agent_id = None
        entrypoint = None
        models = set()

        # Token 累计
        session_input_tokens = 0
        session_output_tokens = 0
        session_cache_tokens = 0
        token_events_count = 0

        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            # ── 提取时间戳 ──
            ts = obj.get("timestamp")
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            sid = obj.get("sessionId")
            if sid:
                session_id = sid

            m = obj.get("mode")
            if m:
                mode = m

            c = obj.get("cwd")
            if c:
                cwd = c

            a_id = obj.get("agentId")
            if a_id:
                agent_id = a_id

            ep = obj.get("entrypoint")
            if ep:
                entrypoint = ep

            # ── 提取 usage（在 assistant message 中）──
            msg = obj.get("message", {})
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                usage = msg.get("usage")
                model = msg.get("model", "")
                if model:
                    models.add(model)

                if usage and isinstance(usage, dict):
                    input_t = usage.get("input_tokens", 0) or 0
                    output_t = usage.get("output_tokens", 0) or 0
                    # cache_read_input_tokens 是缓存命中（复用）
                    cache_t = (usage.get("cache_read_input_tokens", 0) or 0)
                    # cache_creation_input_tokens 是新写入缓存的
                    cache_create = (usage.get("cache_creation_input_tokens", 0) or 0)
                    # 总缓存 = 命中 + 新写入
                    total_cache = cache_t + cache_create

                    session_input_tokens += input_t
                    session_output_tokens += output_t
                    session_cache_tokens += total_cache
                    token_events_count += 1

                    # 记录活动事件
                    if ts:
                        all_activity.append({
                            "ts": ts,
                            "session_id": sid or fp.stem,
                            "date": "",
                            "tool": "claude-code",
                        })

        if not first_ts:
            continue

        # 转换时间戳
        try:
            if isinstance(first_ts, (int, float)):
                dt = datetime.datetime.fromtimestamp(first_ts / 1000, tz=datetime.timezone.utc)
            else:
                dt = datetime.datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        try:
            if isinstance(last_ts, (int, float)):
                end_dt = datetime.datetime.fromtimestamp(last_ts / 1000, tz=datetime.timezone.utc)
            else:
                end_dt = datetime.datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            end_str = end_dt.isoformat()
        except Exception:
            end_str = first_ts if isinstance(first_ts, str) else ""

        # 为所有 activity event 补上日期
        for ev in all_activity:
            if not ev.get("date"):
                ev["date"] = date_str

        # 构建标签
        labels = []
        if is_subagent:
            labels.append("子代理")
            if agent_id:
                labels.append(f"Agent:{agent_id[:8]}")
        else:
            labels.append("主会话")

        if mode:
            labels.append(f"模式:{mode}")
        if entrypoint:
            labels.append(f"入口:{entrypoint[:30]}")
        if token_events_count > 0:
            labels.append(f"{token_events_count}次API请求")

        session_total = session_input_tokens + session_output_tokens + session_cache_tokens

        sid = session_id or fp.stem
        session_info = {
            "id": sid[:30] + ("..." if len(str(sid)) > 30 else ""),
            "workspace": extract_workspace_name(cwd),
            "models": list(models),
            "labels": labels,
            "input_tokens": session_input_tokens,
            "output_tokens": session_output_tokens,
            "cache_tokens": session_cache_tokens,
            "reasoning_tokens": 0,
            "total_tokens": session_total,
            "start_ts": first_ts if isinstance(first_ts, str) else datetime.datetime.fromtimestamp(first_ts / 1000, tz=datetime.timezone.utc).isoformat(),
            "end_ts": end_str,
        }

        # 累计到日期
        daily_data[date_str]["input_tokens"] += session_input_tokens
        daily_data[date_str]["output_tokens"] += session_output_tokens
        daily_data[date_str]["cache_tokens"] += session_cache_tokens
        daily_data[date_str]["total_tokens"] += session_total
        daily_data[date_str]["sessions"].append(session_info)
        daily_data[date_str]["record_units"] += 1

        # 修正 activity event 的日期
        for ev in all_activity:
            if not ev.get("date") or ev["date"] == "":
                ev["date"] = date_str

    return dict(daily_data), all_activity


def parse_cursor_activity():
    """解析 Cursor AI 追踪数据，优先从 state.vscdb 获取 composer headers"""
    all_activity = []
    daily_data = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "record_units": 0,
        "sessions": [],
    })

    # ── 首先尝试从 state.vscdb 获取 composerHeaders ──
    cursor_state_db = find_cursor_state_db()

    if cursor_state_db:
        try:
            conn = sqlite3.connect(str(cursor_state_db))
            cc = conn.cursor()
            cc.execute("SELECT composerId, createdAt, lastUpdatedAt, isSubagent, value FROM composerHeaders ORDER BY createdAt DESC")
            composer_rows = cc.fetchall()
            conn.close()

            for composerId, createdAt, lastUpdatedAt, isSubagent, value in composer_rows:
                if not createdAt:
                    continue

                try:
                    dt = datetime.datetime.fromtimestamp(createdAt / 1000, tz=datetime.timezone.utc)
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    continue

                # 解析 value JSON
                meta = {}
                if value:
                    try:
                        meta = json.loads(value)
                    except Exception:
                        pass

                unified_mode = meta.get("unifiedMode", "")
                lines_added = meta.get("totalLinesAdded", 0) or 0
                lines_removed = meta.get("totalLinesRemoved", 0) or 0
                files_changed = meta.get("filesChangedCount", 0) or 0
                context_usage = meta.get("contextUsagePercent", 0) or 0

                # 安全标签
                # 注意：不采集 name / subtitle 字段——它们由对话内容生成，
                # 保留会构成"保存对话内容"，与本工具只记结构信息的定位不符。
                labels = []
                if isSubagent:
                    labels.append("子代理")
                else:
                    labels.append("主会话")
                if unified_mode:
                    labels.append(f"模式:{unified_mode}")
                if lines_added > 0 or lines_removed > 0:
                    labels.append(f"+{lines_added}/-{lines_removed}行")
                if files_changed > 0:
                    labels.append(f"{files_changed}文件变更")
                if context_usage > 0:
                    labels.append(f"上下文:{context_usage:.0f}%")

                # 工作区名（只取仓库目录名，不取对话标题）
                workspace = "未知"
                tracked_repos = meta.get("trackedGitRepos", [])
                if tracked_repos:
                    try:
                        repo_path = tracked_repos[0].get("repoPath", "")
                        # 复用同一套归一化逻辑（家目录 -> 主目录）
                        workspace = extract_workspace_name(repo_path)
                    except Exception:
                        pass

                end_dt = datetime.datetime.fromtimestamp(
                    (lastUpdatedAt or createdAt) / 1000, tz=datetime.timezone.utc
                )

                session_info = {
                    "id": composerId[:30] + ("..." if len(composerId) > 30 else ""),
                    "workspace": workspace,
                    "models": [],  # Cursor 不在 composerHeaders 存模型
                    "labels": labels,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "start_ts": dt.isoformat(),
                    "end_ts": end_dt.isoformat(),
                    "token_status": "未适配/需要确认",
                }

                daily_data[date_str]["sessions"].append(session_info)
                daily_data[date_str]["record_units"] += 1

                all_activity.append({
                    "ts": dt.isoformat(),
                    "session_id": composerId,
                    "date": date_str,
                    "tool": "cursor",
                })
        except Exception:
            pass

    # ── 回退：从 ai-code-tracking.db 获取补充数据 ──
    db_path = find_cursor_db()
    if db_path:
        try:
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            c.execute("SELECT DISTINCT date(timestamp/1000, 'unixepoch') as d, COUNT(*) as cnt, GROUP_CONCAT(DISTINCT model) as models FROM ai_code_hashes GROUP BY d ORDER BY d")
            rows = c.fetchall()
            conn.close()

            # 合并模型信息到已有 sessions
            for date_str, cnt, models in rows:
                if not date_str:
                    continue
                models_list = models.split(",") if models else []
                if date_str in daily_data:
                    for s in daily_data[date_str]["sessions"]:
                        if not s["models"]:
                            s["models"] = models_list
        except Exception:
            pass

    return dict(daily_data), all_activity


def compute_activity_rhythm(activity_events, date_str):
    """
    根据 Token 事件时间，计算某天的 AI 工作节奏。
    - 首个/最后活动时间
    - 估算 AI 活跃时长
    - 活动片段（≤30分钟间隔合并）
    - 每小时 Token 高峰
    - 峰值并发记录单元数
    """
    # 筛选当天事件
    day_events = [e for e in activity_events if e.get("date") == date_str]
    if not day_events:
        return None

    # 按时间排序
    day_events.sort(key=lambda e: e["ts"])

    # 提取时间和 session
    times = []
    for ev in day_events:
        try:
            if isinstance(ev["ts"], str):
                dt = datetime.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
            else:
                dt = datetime.datetime.fromtimestamp(ev["ts"] / 1000, tz=datetime.timezone.utc)
            times.append({
                "dt": dt,
                "session_id": ev.get("session_id", ""),
                "tool": ev.get("tool", ""),
            })
        except Exception:
            continue

    if not times:
        return None

    times.sort(key=lambda t: t["dt"])

    first_time = times[0]["dt"]
    last_time = times[-1]["dt"]

    first_str = first_time.strftime("%H:%M")
    last_str = last_time.strftime("%H:%M")

    # 活动片段合并（≤30分钟间隔合并）
    segments = []
    seg_start = times[0]["dt"]
    seg_end = times[0]["dt"]

    for i in range(1, len(times)):
        gap = (times[i]["dt"] - times[i-1]["dt"]).total_seconds() / 60
        if gap <= 30:
            seg_end = times[i]["dt"]
        else:
            segments.append({
                "start": seg_start.strftime("%H:%M"),
                "end": seg_end.strftime("%H:%M"),
                "duration_min": round((seg_end - seg_start).total_seconds() / 60),
            })
            seg_start = times[i]["dt"]
            seg_end = times[i]["dt"]
    segments.append({
        "start": seg_start.strftime("%H:%M"),
        "end": seg_end.strftime("%H:%M"),
        "duration_min": round((seg_end - seg_start).total_seconds() / 60),
    })

    # 估算活跃时长：每个 Token 事件 ≈ 5 分钟，但合并后取片段时长
    estimated_active_min = sum(s["duration_min"] for s in segments)
    if estimated_active_min == 0:
        estimated_active_min = len(times) * 5  # 每个事件至少算5分钟

    # 每小时分布
    hourly = defaultdict(int)
    for t in times:
        hour = t["dt"].hour
        hourly[hour] += 1

    hourly_peaks = []
    for hour, count in sorted(hourly.items()):
        hourly_peaks.append({"hour": f"{hour:02d}:00", "events": count})

    # 峰值并发：用 session 重叠计算
    session_spans = defaultdict(list)
    for t in times:
        session_spans[t["session_id"]].append(t["dt"])

    # 简化：统计同时活跃的 session 数（在 5 分钟窗口内）
    max_concurrent = 1
    for i, t in enumerate(times):
        window_end = t["dt"] + datetime.timedelta(minutes=5)
        concurrent = 1
        for j, t2 in enumerate(times):
            if i != j and t["dt"] <= t2["dt"] <= window_end:
                concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)

    return {
        "first_activity": first_str,
        "last_activity": last_str,
        "estimated_active_minutes": estimated_active_min,
        "activity_segments": segments,
        "hourly_peaks": hourly_peaks,
        "peak_concurrent_units": max_concurrent,
        "total_events": len(times),
        "unique_sessions": len(set(t["session_id"] for t in times)),
        "note": "AI活动推算，不等于真人实际工时",
    }


def sync_all():
    """主同步函数"""
    now = datetime.datetime.now(datetime.timezone.utc)
    now_str = now.isoformat()

    print("=" * 60)
    print("本地 AI Token 账本 — 数据同步")
    print(f"同步时间: {now_str}")
    print("=" * 60)

    # ── 1. 解析 Codex ──────────────────────────────
    print("\n📊 扫描 Codex sessions...")
    codex_daily, codex_activity = parse_codex_sessions()
    print(f"   发现 {len(codex_activity)} 条 Token 事件，覆盖 {len(codex_daily)} 天")

    # ── 2. 解析 Claude Code ────────────────────────
    print("\n📊 扫描 Claude Code 活动记录...")
    claude_daily, claude_activity = parse_claude_activity()
    print(f"   发现 {len(claude_activity)} 条活动记录，覆盖 {len(claude_daily)} 天")

    # ── 3. 解析 Cursor ─────────────────────────────
    print("\n📊 扫描 Cursor 活动记录...")
    cursor_daily, cursor_activity = parse_cursor_activity()
    print(f"   发现 {len(cursor_activity)} 条活动记录，覆盖 {len(cursor_daily)} 天")

    # ── 4. 合并数据 ─────────────────────────────────
    all_dates = set()
    all_dates.update(codex_daily.keys())
    all_dates.update(claude_daily.keys())
    all_dates.update(cursor_daily.keys())
    all_dates = sorted(all_dates)

    all_activity = codex_activity + claude_activity + cursor_activity

    # ── 5. 按日汇总 ─────────────────────────────────
    tool_definitions = {
        "codex": {
            "name": "Codex",
            "status": "supported",
            "record_unit": "会话日志",
            "record_unit_desc": "当天有 Token 活动的会话日志文件（~/.codex/sessions/**/*.jsonl）",
            "token_source": "token_count 事件的 total_token_usage 字段按时间序列增量计算",
        },
        "claude-code": {
            "name": "Claude Code",
            "status": "supported",
            "record_unit": "对话文件",
            "record_unit_desc": "当天有 Token 活动的对话 JSONL 文件（~/.claude/projects/**/*.jsonl，主会话或子代理）",
            "token_source": "assistant message 的 usage 字段（input / output / cache_read / cache_creation），按单次请求值求和",
        },
        "cursor": {
            "name": "Cursor",
            "status": "activity_only",
            "record_unit": "会话",
            "record_unit_desc": "当天有活动的 Composer 会话（来自 state.vscdb 的 composerHeaders）",
            "token_source": "Cursor 未在本地库中暴露 Token 字段，故只统计会话数与时间分布，不统计 Token",
            "note": "Token统计未适配",
        },
    }

    daily_records = []
    grand_totals = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "record_units": 0,
        "active_days": 0,
    })

    for date_str in all_dates:
        day_entry = {"date": date_str, "tools": {}}

        for tool_key, tool_daily in [
            ("codex", codex_daily),
            ("claude-code", claude_daily),
            ("cursor", cursor_daily),
        ]:
            td = tool_daily.get(date_str)
            if td and (td["total_tokens"] > 0 or td["record_units"] > 0):
                # 计算活动节奏（只对有 token 的工具）
                rhythm = None
                if tool_key == "codex":
                    rhythm = compute_activity_rhythm(
                        [e for e in all_activity if e["date"] == date_str and e["tool"] == tool_key],
                        date_str
                    )

                day_entry["tools"][tool_key] = {
                    "input_tokens": td["input_tokens"],
                    "output_tokens": td["output_tokens"],
                    "cache_tokens": td["cache_tokens"],
                    "reasoning_tokens": td["reasoning_tokens"],
                    "total_tokens": td["total_tokens"],
                    "record_units": td["record_units"],
                    "sessions": td["sessions"],
                    "activity_rhythm": rhythm,
                }

                # 累计
                gt = grand_totals[tool_key]
                gt["input_tokens"] += td["input_tokens"]
                gt["output_tokens"] += td["output_tokens"]
                gt["cache_tokens"] += td["cache_tokens"]
                gt["reasoning_tokens"] += td["reasoning_tokens"]
                gt["total_tokens"] += td["total_tokens"]
                gt["record_units"] += td["record_units"]
                gt["active_days"] += 1

        if day_entry["tools"]:
            daily_records.append(day_entry)

    # ── 6. 计算全局活动节奏 ────────────────────────
    def compute_global_daily_rhythm(date_str):
        """计算某天所有工具的合并活动节奏"""
        day_events = [e for e in all_activity if e.get("date") == date_str]
        return compute_activity_rhythm(day_events, date_str) if day_events else None

    for day_entry in daily_records:
        day_entry["combined_rhythm"] = compute_global_daily_rhythm(day_entry["date"])

    # ── 7. 计算汇总统计 ─────────────────────────────
    total_all_tokens = sum(gt["total_tokens"] for gt in grand_totals.values())
    total_all_input = sum(gt["input_tokens"] for gt in grand_totals.values())
    total_all_output = sum(gt["output_tokens"] for gt in grand_totals.values())
    total_all_cache = sum(gt["cache_tokens"] for gt in grand_totals.values())
    total_all_reasoning = sum(gt["reasoning_tokens"] for gt in grand_totals.values())
    total_all_units = sum(gt["record_units"] for gt in grand_totals.values())

    active_dates = sorted(set(
        d for d in all_dates
        if any(
            t in codex_daily or t in claude_daily or t in cursor_daily
            for t in [d]
        )
    ))

    active_days_count = len(all_dates)

    # 今日数据
    today_str = now.strftime("%Y-%m-%d")
    today_entry = None
    for de in daily_records:
        if de["date"] == today_str:
            today_entry = de
            break

    today_tokens = sum(
        t.get("total_tokens", 0)
        for t in (today_entry["tools"].values() if today_entry else [])
    )

    # ── 8. 组装输出 ─────────────────────────────────
    output = {
        "meta": {
            "last_sync": now_str,
            "version": "1.0",
        },
        "date_range": {
            "first": all_dates[0] if all_dates else None,
            "last": all_dates[-1] if all_dates else None,
            "active_days": active_days_count,
        },
        "tools": tool_definitions,
        "totals": {
            tool_key: {
                "input_tokens": gt["input_tokens"],
                "output_tokens": gt["output_tokens"],
                "cache_tokens": gt["cache_tokens"],
                "reasoning_tokens": gt["reasoning_tokens"],
                "total_tokens": gt["total_tokens"],
                "record_units": gt["record_units"],
                "active_days": gt["active_days"],
            }
            for tool_key, gt in grand_totals.items()
        },
        "summary": {
            "all_total_tokens": total_all_tokens,
            "all_input_tokens": total_all_input,
            "all_output_tokens": total_all_output,
            "all_cache_tokens": total_all_cache,
            "all_reasoning_tokens": total_all_reasoning,
            "all_record_units": total_all_units,
            "active_days": active_days_count,
            "today_tokens": today_tokens,
            "avg_daily_tokens": round(total_all_tokens / active_days_count) if active_days_count > 0 else 0,
        },
        "daily": daily_records,
    }

    # ── 9. 写 JSON ──────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据已写入: {DATA_FILE}")

    # ── 10. 生成仪表盘 ──────────────────────────────
    generate_dashboard(output)

    # ── 11. 汇总报告 ────────────────────────────────
    print("\n" + "=" * 60)
    print("同步汇总")
    print("=" * 60)
    print(f"活跃天数: {active_days_count}")
    print(f"日期范围: {all_dates[0] if all_dates else 'N/A'} ~ {all_dates[-1] if all_dates else 'N/A'}")
    print(f"累计 Token: {total_all_tokens:,}")
    print(f"  - 输入: {total_all_input:,}")
    print(f"  - 输出: {total_all_output:,}")
    print(f"  - 缓存复用: {total_all_cache:,}")
    print(f"  - 推理: {total_all_reasoning:,}")
    print(f"今日 Token: {today_tokens:,}")
    print(f"日均 Token: {round(total_all_tokens / active_days_count):,}" if active_days_count > 0 else "日均 Token: N/A")
    print(f"总记录单元: {total_all_units}")
    print()
    for tool_key, gt in grand_totals.items():
        name = tool_definitions.get(tool_key, {}).get("name", tool_key)
        status = tool_definitions.get(tool_key, {}).get("status", "unknown")
        print(f"  {name}: {gt['total_tokens']:,} tokens | {gt['record_units']} units | {gt['active_days']} days | 状态: {status}")

    return output


def build_footer_notes(tool_definitions):
    """从工具定义生成页脚统计口径说明，避免页脚与代码脱节。"""
    lines = []
    for key, td in tool_definitions.items():
        name = td.get("name", key)
        parts = []
        if td.get("record_unit_desc"):
            parts.append(f"记录单元 = {td['record_unit_desc']}")
        if td.get("token_source"):
            parts.append(f"Token 来源：{td['token_source']}")
        if td.get("note"):
            parts.append(td["note"])
        if parts:
            lines.append(f"<p>• <b>{name}</b>：{'；'.join(parts)}。</p>")
    return "\n            ".join(lines)


def generate_dashboard(data, out_file=DASHBOARD_FILE):
    """生成自包含的 dashboard.html"""

    # 将数据嵌入为 JavaScript 变量
    data_json = json.dumps(data, ensure_ascii=False)
    footer_notes = build_footer_notes(data.get("tools", {}))

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>本地 AI Token 账本</title>
<style>
/* ── 基础样式 ────────────────────────────── */
:root {{
  --bg: #0d1117;
  --bg-card: #161b22;
  --bg-card-hover: #1c2333;
  --border: #30363d;
  --text: #e6edf3;
  --text-secondary: #8b949e;
  --accent: #58a6ff;
  --accent-green: #3fb950;
  --accent-orange: #d2991d;
  --accent-purple: #a371f7;
  --accent-red: #f85149;
  --codex-color: #58a6ff;
  --claude-color: #d2991d;
  --cursor-color: #a371f7;
  --radius: 8px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  min-height: 100vh;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}

/* ── 头部 ────────────────────────────────── */
.header {{
  text-align: center;
  padding: 40px 0 30px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 30px;
}}
.header h1 {{ font-size: 2em; font-weight: 700; margin-bottom: 8px; }}
.header .subtitle {{ color: var(--text-secondary); font-size: 0.9em; }}
.header .sync-time {{ color: var(--text-secondary); font-size: 0.8em; margin-top: 4px; }}

/* ── 统计卡片 ────────────────────────────── */
.stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 30px;
}}
.stat-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  text-align: center;
}}
.stat-card .value {{
  font-size: 2em;
  font-weight: 700;
  color: var(--accent);
}}
.stat-card .label {{ color: var(--text-secondary); font-size: 0.85em; margin-top: 4px; }}
.stat-card.today .value {{ color: var(--accent-green); }}
.stat-card.avg .value {{ color: var(--accent-orange); }}
.stat-card.days .value {{ color: var(--accent-purple); }}

/* ── 分区标题 ────────────────────────────── */
.section-title {{
  font-size: 1.3em;
  font-weight: 600;
  margin: 36px 0 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}}

/* ── 趋势图 ──────────────────────────────── */
.chart-container {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  overflow-x: auto;
  margin-bottom: 24px;
}}
.chart-container h3 {{ margin-bottom: 16px; font-size: 1em; color: var(--text-secondary); }}
.bar-chart {{ display: flex; align-items: flex-end; gap: 4px; min-height: 180px; padding: 10px 0; }}
.bar-group {{ display: flex; flex-direction: column; align-items: center; flex: 1; min-width: 28px; }}
.bar {{ width: 100%; max-width: 40px; border-radius: 3px 3px 0 0; transition: opacity 0.2s; cursor: pointer; }}
.bar:hover {{ opacity: 0.8; }}
.bar.codex {{ background: var(--codex-color); }}
.bar.claude-code {{ background: var(--claude-color); }}
.bar.cursor {{ background: var(--cursor-color); }}
.bar-label {{ font-size: 0.65em; color: var(--text-secondary); margin-top: 6px; writing-mode: vertical-lr; }}
.bar-value {{ font-size: 0.6em; color: var(--text-secondary); margin-bottom: 2px; }}

/* ── 工具分布 ─────────────────────────────── */
.distribution {{ display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }}
.dist-row {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.dist-label {{
  min-width: 120px;
  font-weight: 600;
}}
.dist-bar-bg {{
  flex: 1;
  height: 32px;
  background: var(--bg);
  border-radius: 4px;
  overflow: hidden;
  position: relative;
}}
.dist-bar-fill {{
  height: 100%;
  border-radius: 4px;
  display: flex;
  align-items: center;
  padding-left: 10px;
  font-size: 0.85em;
  font-weight: 600;
  color: #fff;
  min-width: fit-content;
}}
.dist-bar-fill.codex {{ background: var(--codex-color); }}
.dist-bar-fill.claude-code {{ background: var(--claude-color); }}
.dist-bar-fill.cursor {{ background: var(--cursor-color); }}
.dist-value {{ min-width: 120px; text-align: right; font-size: 0.9em; color: var(--text-secondary); }}

/* ── 工作节奏 ─────────────────────────────── */
.rhythm-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  margin-bottom: 16px;
}}
.rhythm-card h4 {{ margin-bottom: 12px; font-size: 1em; }}
.rhythm-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}}
.rhythm-item {{
  background: var(--bg);
  border-radius: 4px;
  padding: 10px 14px;
  text-align: center;
}}
.rhythm-item .rv {{ font-size: 1.4em; font-weight: 700; color: var(--accent); }}
.rhythm-item .rl {{ font-size: 0.75em; color: var(--text-secondary); }}

/* 活动片段 */
.segment-bar {{
  display: flex;
  height: 24px;
  background: var(--bg);
  border-radius: 4px;
  overflow: hidden;
  margin: 8px 0;
  position: relative;
}}
.segment-fill {{
  background: var(--accent-green);
  opacity: 0.6;
  border-radius: 2px;
  position: absolute;
  height: 100%;
}}
.segment-label {{
  font-size: 0.7em;
  color: var(--text-secondary);
  margin-top: 2px;
}}

/* 小时热力 */
.hourly-grid {{
  display: flex;
  gap: 3px;
  flex-wrap: wrap;
  margin: 8px 0;
}}
.hourly-cell {{
  width: 32px;
  height: 32px;
  border-radius: 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.55em;
  color: var(--text);
  position: relative;
}}
.hourly-cell .hour-label {{ position: absolute; bottom: -14px; font-size: 0.85em; color: var(--text-secondary); }}

/* ── 每日明细 ─────────────────────────────── */
.daily-detail {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 12px;
  overflow: hidden;
}}
.daily-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  cursor: pointer;
  transition: background 0.2s;
}}
.daily-header:hover {{ background: var(--bg-card-hover); }}
.daily-header .date {{ font-weight: 600; font-size: 1.05em; }}
.daily-header .day-tokens {{ color: var(--accent); font-weight: 600; }}
.daily-header .day-meta {{ color: var(--text-secondary); font-size: 0.85em; }}
.daily-body {{
  display: none;
  padding: 0 20px 16px;
  border-top: 1px solid var(--border);
}}
.daily-body.open {{ display: block; }}

/* 工具子区块 */
.tool-section {{
  margin: 16px 0;
  padding: 12px;
  background: var(--bg);
  border-radius: var(--radius);
}}
.tool-section .tool-name {{
  font-weight: 600;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.tool-badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 0.7em;
  font-weight: 600;
}}
.tool-badge.codex {{ background: rgba(88,166,255,0.15); color: var(--codex-color); }}
.tool-badge.claude-code {{ background: rgba(210,153,29,0.15); color: var(--claude-color); }}
.tool-badge.cursor {{ background: rgba(163,113,247,0.15); color: var(--cursor-color); }}

/* Session 列表 */
.session-list {{ margin: 8px 0; }}
.session-item {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 0.85em;
}}
.session-item:last-child {{ border-bottom: none; }}
.session-item .sess-info {{ flex: 1; }}
.session-item .sess-tokens {{ text-align: right; color: var(--accent); font-weight: 600; }}
.session-item .sess-labels {{ display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }}
.sess-label {{
  display: inline-block;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 0.7em;
  background: var(--border);
  color: var(--text-secondary);
}}

/* Token状态提示 */
.token-na {{
  color: var(--accent-orange);
  font-size: 0.8em;
  font-style: italic;
}}

/* ── 状态提示 ────────────────────────────── */
.status-banner {{
  background: rgba(210,153,29,0.1);
  border: 1px solid rgba(210,153,29,0.3);
  border-radius: var(--radius);
  padding: 12px 16px;
  margin-bottom: 20px;
  font-size: 0.85em;
  color: var(--accent-orange);
}}

/* ── 页脚 ────────────────────────────────── */
.footer {{
  text-align: center;
  padding: 40px 0 20px;
  color: var(--text-secondary);
  font-size: 0.8em;
  border-top: 1px solid var(--border);
  margin-top: 40px;
}}
.footer .notes {{ max-width: 600px; margin: 12px auto 0; font-size: 0.75em; line-height: 1.5; }}

/* ── 响应式 ──────────────────────────────── */
@media (max-width: 600px) {{
  .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .container {{ padding: 10px; }}
  .daily-header {{ flex-direction: column; align-items: flex-start; gap: 4px; }}
}}

/* 工具状态标记 */
.status-dot {{
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}}
.status-dot.ok {{ background: var(--accent-green); }}
.status-dot.partial {{ background: var(--accent-orange); }}
.status-dot.off {{ background: var(--accent-red); }}
</style>
</head>
<body>
<div class="container" id="app"></div>

<script>
// ── 数据 ──────────────────────────────────────
const DATA = {data_json};

// ── 工具函数 ──────────────────────────────────
function fmtNum(n) {{
    if (n === null || n === undefined) return "0";
    if (n < 10000) return n.toLocaleString("zh-CN");
    if (n < 100000000) return (n / 10000).toFixed(1) + " 万";
    return (n / 100000000).toFixed(2) + " 亿";
}}

function fmtNumFull(n) {{
    if (n === null || n === undefined) return "0";
    return n.toLocaleString("zh-CN");
}}

function toolColor(tool) {{
    if (tool === "codex") return "codex";
    if (tool === "claude-code") return "claude-code";
    if (tool === "cursor") return "cursor";
    return "";
}}

function toolName(tool) {{
    const map = {{ "codex": "Codex", "claude-code": "Claude Code", "cursor": "Cursor" }};
    return map[tool] || tool;
}}

// ── 渲染 ──────────────────────────────────────
function render() {{
    const app = document.getElementById("app");
    const m = DATA.meta;
    const s = DATA.summary;
    const dr = DATA.date_range;

    // 状态提示
    let statusMsg = "";
    for (const [k, t] of Object.entries(DATA.tools)) {{
        if (t.status === "not_installed") statusMsg += `<b>${{t.name}}</b>: 未安装；`;
        if (t.status === "no_data") statusMsg += `<b>${{t.name}}</b>: 无使用数据；`;
        if (t.status === "activity_only") statusMsg += `<b>${{t.name}}</b>: Token未适配；`;
    }}

    app.innerHTML = `
    <div class="header">
        <h1>📊 本地 AI Token 账本</h1>
        <div class="subtitle">仅读取本机数据 · 不上传任何服务器 · 每日自动聚合</div>
        <div class="sync-time">最后同步: ${{new Date(m.last_sync).toLocaleString("zh-CN")}}</div>
    </div>

    ${{DATA.meta.sample ? `<div class="status-banner">⚠️ 这是脱敏示例数据，并非真实用量。运行 run.ps1 / run.sh 即可生成你自己的仪表盘。</div>` : ""}}
    ${{statusMsg ? `<div class="status-banner">⚠️ ${{statusMsg}}如需完整统计，请在对应工具中使用后回来同步。</div>` : ""}}

    <!-- 统计卡片 -->
    <div class="stats-grid">
        <div class="stat-card">
            <div class="value">${{fmtNum(s.all_total_tokens)}}</div>
            <div class="label">累计 Token</div>
        </div>
        <div class="stat-card today">
            <div class="value">${{fmtNum(s.today_tokens)}}</div>
            <div class="label">今日 Token</div>
        </div>
        <div class="stat-card avg">
            <div class="value">${{fmtNum(s.avg_daily_tokens)}}</div>
            <div class="label">日均 Token</div>
        </div>
        <div class="stat-card days">
            <div class="value">${{s.active_days}}</div>
            <div class="label">活跃天数</div>
        </div>
    </div>

    <!-- 14天趋势 -->
    <div class="chart-container">
        <h3>📈 近 14 天 Token 趋势</h3>
        ${{renderTrendChart()}}
    </div>

    <!-- 工具分布 -->
    <div class="section-title">🔧 按工具分布</div>
    ${{renderDistribution()}}

    <!-- Token 明细 -->
    <div class="section-title">📋 Token 明细</div>
    ${{renderTokenBreakdown()}}

    <!-- AI 工作节奏 -->
    <div class="section-title">⏱️ AI 工作节奏（最近7天）</div>
    ${{renderRhythm()}}

    <!-- 每日明细 -->
    <div class="section-title">📅 每日明细</div>
    ${{renderDailyDetails()}}

    <!-- 页脚 -->
    <div class="footer">
        <p>本地 AI Token 账本 v1.0 · 数据来源：本机文件 · 无网络请求</p>
        <div class="notes">
            <p><b>统计口径说明：</b></p>
            {footer_notes}
            <p>• <b>并发</b>：仅表示记录单元的活动时段重叠，不等于实时并发请求。</p>
            <p>• <b>AI 活跃时长</b>：基于 Token 事件时间和活动片段推算（≤30分钟间隔合并，单事件按5分钟计），<b>不等于真人实际工时</b>。</p>
            <p>• <b>隐私</b>：本页所有数据由本机脚本生成并内嵌于本文件，不发起任何网络请求。请勿把本次生成的 dashboard.html 提交到公开仓库。</p>
        </div>
    </div>
    `;
}}

function renderTrendChart() {{
    const days = DATA.daily.slice(-14);
    if (days.length === 0) return '<p style="color:var(--text-secondary)">暂无数据</p>';

    const maxTokens = Math.max(...days.map(d => {{
        let total = 0;
        for (const t of Object.values(d.tools || {{}})) total += t.total_tokens || 0;
        return total;
    }}), 1);

    let bars = "";
    for (const day of days) {{
        let dayTotal = 0;
        let toolBars = "";
        for (const [tk, td] of Object.entries(day.tools || {{}})) {{
            if ((td.total_tokens || 0) > 0) {{
                const h = Math.max(3, (td.total_tokens / maxTokens) * 160);
                toolBars += `<div class="bar ${{toolColor(tk)}}" style="height:${{h}}px" title="${{toolName(tk)}}: ${{fmtNumFull(td.total_tokens)}} tokens"></div>`;
                dayTotal += td.total_tokens || 0;
            }}
        }}
        if (dayTotal === 0) {{
            toolBars = `<div class="bar" style="height:4px;background:var(--border)" title="无Token"></div>`;
        }}
        const dateLabel = day.date.slice(5);
        bars += `
            <div class="bar-group">
                <div class="bar-value">${{dayTotal > 0 ? fmtNum(dayTotal) : ""}}</div>
                <div style="display:flex;gap:2px;align-items:flex-end">${{toolBars}}</div>
                <div class="bar-label">${{dateLabel}}</div>
            </div>`;
    }}
    return `<div class="bar-chart">${{bars}}</div>`;
}}

function renderDistribution() {{
    const totals = DATA.totals;
    const maxVal = Math.max(...Object.values(totals).map(t => t.total_tokens || 0), 1);

    let rows = "";
    for (const [tk, td] of Object.entries(totals)) {{
        if ((td.total_tokens || 0) === 0 && (td.record_units || 0) === 0) continue;
        const pct = Math.max(1, ((td.total_tokens || 0) / maxVal) * 100);
        const statusIcon = DATA.tools[tk]?.status === "activity_only"
            ? '<span class="status-dot partial"></span>'
            : '<span class="status-dot ok"></span>';
        rows += `
            <div class="dist-row">
                <div class="dist-label">${{statusIcon}}${{toolName(tk)}}</div>
                <div class="dist-bar-bg">
                    <div class="dist-bar-fill ${{toolColor(tk)}}" style="width:${{pct}}%">
                        ${{pct > 10 ? fmtNum(td.total_tokens || 0) : ""}}
                    </div>
                </div>
                <div class="dist-value">
                    ${{fmtNum(td.total_tokens || 0)}} tokens · ${{td.record_units || 0}} 单元
                </div>
            </div>`;
    }}
    return `<div class="distribution">${{rows}}</div>`;
}}

function renderTokenBreakdown() {{
    const totals = DATA.totals;
    let html = '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:0.9em">';
    html += '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:8px">工具</th><th style="text-align:right;padding:8px">输入</th><th style="text-align:right;padding:8px">输出</th><th style="text-align:right;padding:8px">缓存复用</th><th style="text-align:right;padding:8px">推理</th><th style="text-align:right;padding:8px">总量</th><th style="text-align:right;padding:8px">状态</th></tr>';
    for (const [tk, td] of Object.entries(totals)) {{
        if (td.total_tokens === 0 && td.record_units === 0) continue;
        const st = DATA.tools[tk]?.status === "activity_only" ? "⚠️ 未适配" : "✅";
        html += `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:8px;font-weight:600">${{toolName(tk)}}</td>
            <td style="text-align:right;padding:8px">${{td.input_tokens > 0 ? fmtNumFull(td.input_tokens) : "-"}}</td>
            <td style="text-align:right;padding:8px">${{td.output_tokens > 0 ? fmtNumFull(td.output_tokens) : "-"}}</td>
            <td style="text-align:right;padding:8px">${{td.cache_tokens > 0 ? fmtNumFull(td.cache_tokens) : "-"}}</td>
            <td style="text-align:right;padding:8px">${{td.reasoning_tokens > 0 ? fmtNumFull(td.reasoning_tokens) : "-"}}</td>
            <td style="text-align:right;padding:8px;font-weight:600;color:var(--accent)">${{fmtNumFull(td.total_tokens)}}</td>
            <td style="text-align:right;padding:8px">${{st}}</td>
        </tr>`;
    }}
    html += '</table></div>';
    return html;
}}

function renderRhythm() {{
    const recentDays = DATA.daily.slice(-7);
    let html = "";
    for (const day of recentDays.reverse()) {{
        const rhythm = day.combined_rhythm;
        if (!rhythm || rhythm.total_events === 0) continue;

        html += `<div class="rhythm-card">
            <h4>${{day.date}} · ${{rhythm.total_events}} 个Token事件 · ${{rhythm.unique_sessions}} 个记录单元</h4>
            <div class="rhythm-grid">
                <div class="rhythm-item"><div class="rv">${{rhythm.first_activity}}</div><div class="rl">首个活动</div></div>
                <div class="rhythm-item"><div class="rv">${{rhythm.last_activity}}</div><div class="rl">最后活动</div></div>
                <div class="rhythm-item"><div class="rv">${{rhythm.estimated_active_minutes}} min</div><div class="rl">估算AI活跃时长</div></div>
                <div class="rhythm-item"><div class="rv">${{rhythm.peak_concurrent_units}}</div><div class="rl">峰值并发单元</div></div>
            </div>`;

        // 活动片段
        if (rhythm.activity_segments && rhythm.activity_segments.length > 0) {{
            html += '<div style="font-size:0.8em;color:var(--text-secondary);margin-bottom:4px">活动片段：</div>';
            const totalMins = Math.max(...rhythm.activity_segments.map(s => s.duration_min), 30);
            html += '<div class="segment-bar" style="position:relative">';
            for (const seg of rhythm.activity_segments) {{
                // 简化：按顺序排列
                const w = Math.max(3, (seg.duration_min / totalMins) * 100);
                html += `<span class="segment-fill" style="width:${{w}}%;position:relative;margin-right:2px" title="${{seg.start}}-${{seg.end}} (${{seg.duration_min}}min)"></span>`;
            }}
            html += '</div>';
            for (const seg of rhythm.activity_segments) {{
                html += `<span class="segment-label">${{seg.start}}-${{seg.end}} (${{seg.duration_min}}min)</span> `;
            }}
        }}

        // 小时分布
        if (rhythm.hourly_peaks && rhythm.hourly_peaks.length > 0) {{
            const maxEvents = Math.max(...rhythm.hourly_peaks.map(h => h.events), 1);
            html += '<div style="font-size:0.8em;color:var(--text-secondary);margin-top:8px">每小时 Token 高峰：</div>';
            html += '<div class="hourly-grid">';
            for (const hp of rhythm.hourly_peaks) {{
                const intensity = Math.min(1, hp.events / maxEvents);
                const r = Math.floor(88 + 167 * intensity);
                const g = Math.floor(166 - 120 * intensity);
                const b = Math.floor(255 - 150 * intensity);
                html += `<div class="hourly-cell" style="background:rgba(${{r}},${{g}},${{b}},${{0.2 + intensity * 0.8}})" title="${{hp.hour}}: ${{hp.events}} 事件"><span class="hour-label">${{hp.hour.slice(0,2)}}</span>${{hp.events}}</div>`;
            }}
            html += '</div>';
        }}

        html += `<div style="font-size:0.7em;color:var(--text-secondary);margin-top:4px">⚠️ AI 活动推算，不等于真人实际工时</div>`;
        html += '</div>';
    }}
    return html || '<p style="color:var(--text-secondary)">暂无活动数据</p>';
}}

function renderDailyDetails() {{
    let html = "";
    for (const day of [...DATA.daily].reverse()) {{
        let dayTokens = 0;
        let dayUnits = 0;
        const toolsHtml = [];

        for (const [tk, td] of Object.entries(day.tools || {{}})) {{
            dayTokens += td.total_tokens || 0;
            dayUnits += td.record_units || 0;

            let sessionsHtml = "";
            for (const sess of (td.sessions || [])) {{
                const labelsHtml = (sess.labels || []).map(l => `<span class="sess-label">${{l}}</span>`).join("");
                const tokenHtml = sess.token_status
                    ? `<span class="token-na">${{sess.token_status}}</span>`
                    : `<span>${{fmtNumFull(sess.total_tokens || 0)}}</span>`;
                const modelsHtml = (sess.models || []).length > 0
                    ? `<span class="sess-label">模型:${{sess.models.join(",")}}</span>`
                    : "";

                sessionsHtml += `
                    <div class="session-item">
                        <div class="sess-info">
                            <div>${{sess.workspace || "未知"}}</div>
                            <div class="sess-labels">${{labelsHtml}}${{modelsHtml}}</div>
                        </div>
                        <div class="sess-tokens">${{tokenHtml}}</div>
                    </div>`;
            }}

            // 活动节奏
            let rhythmHtml = "";
            const rhythm = td.activity_rhythm;
            if (rhythm) {{
                rhythmHtml = `
                    <div style="font-size:0.8em;color:var(--text-secondary);margin-top:8px;padding:8px;background:var(--bg-card);border-radius:4px">
                        🕐 ${{rhythm.first_activity}} ~ ${{rhythm.last_activity}} ·
                        ⏱️ 预估活跃 ${{rhythm.estimated_active_minutes}}min ·
                        📊 ${{rhythm.total_events}}事件 ·
                        🔗 峰值并发 ${{rhythm.peak_concurrent_units}}
                        ${{rhythm.activity_segments ? '· 片段: ' + rhythm.activity_segments.map(s => s.start + '-' + s.end).join(', ') : ''}}
                    </div>`;
            }}

            const badgeClass = toolColor(tk);
            toolsHtml.push(`
                <div class="tool-section">
                    <div class="tool-name">
                        <span class="tool-badge ${{badgeClass}}">${{toolName(tk)}}</span>
                        <span style="font-size:0.85em;color:var(--text-secondary)">
                            ${{fmtNumFull(td.total_tokens || 0)}} tokens ·
                            ${{td.record_units || 0}} 单元
                            ${{DATA.tools[tk]?.record_unit ? '（' + DATA.tools[tk].record_unit + '）' : ''}}
                        </span>
                    </div>
                    ${{rhythmHtml}}
                    <div class="session-list">${{sessionsHtml}}</div>
                </div>
            `);
        }}

        html += `
            <div class="daily-detail">
                <div class="daily-header" onclick="this.nextElementSibling.classList.toggle('open')">
                    <span class="date">📅 ${{day.date}}</span>
                    <span class="day-meta">${{dayUnits}} 记录单元</span>
                    <span class="day-tokens">${{fmtNumFull(dayTokens)}} tokens</span>
                </div>
                <div class="daily-body">
                    ${{toolsHtml.join("")}}
                </div>
            </div>`;
    }}
    return html;
}}

// ── 启动 ──────────────────────────────────────
render();
</script>
</body>
</html>'''

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 仪表盘已生成: {out_file}")


def render_sample():
    """用脱敏示例数据生成演示仪表盘，不读取本机任何真实日志。"""
    if not SAMPLE_FILE.exists():
        print(f"❌ 未找到示例数据: {SAMPLE_FILE}")
        return 1
    data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    data.setdefault("meta", {})["sample"] = True
    generate_dashboard(data, out_file=SAMPLE_DASHBOARD_FILE)
    print("\n这是演示数据，不含你的任何真实记录。")
    print(f"打开: {SAMPLE_DASHBOARD_FILE}")
    return 0


# ── 入口 ──────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print("用法: python sync_tokens.py [--sample]")
        print()
        print("  无参数      扫描本机 Codex / Claude Code / Cursor 日志，"
              "生成 data/usage.json 与 dashboard.html")
        print("  --sample    用 data/usage.sample.json 生成脱敏演示页 "
              "dashboard.sample.html，不读取本机日志")
        sys.exit(0)
    if args and args[0] == "--sample":
        sys.exit(render_sample())
    sync_all()
