"""会话历史与续聊，两部分都基于 openjiuwen 自带的能力：

1. 历史记录（给人看）：openjiuwen.harness.cli.storage 的 SessionStore 格式——每个会话一个 JSON 文件，
   内容是 StoredSession / StoredMessage。SDK 的 SessionStore 只能新建会话、追加消息、列出摘要
   （按文件名排序），不能读详情，也不能接着写已有会话。Mole 补上：
   - 按项目分目录：~/.mole-agent/sessions/<项目名>-<路径哈希>/，/history 只列当前项目的会话；
   - 列表按最近活动排序，标题取第一条用户输入；读取单个会话的详情；
   - 续聊时接着写原来的文件（直接用 SDK 的 StoredSession / StoredMessage 读写，格式不变）。
   消息的 role：user / assistant / system 是 SDK 约定的；Mole 另外用 tool 记录工具调用的一行摘要。

2. 对话上下文（给 agent 用）：SDK 的 PersistenceCheckpointer 把每个会话的 agent 状态（包括上下文里的
   全部消息、待确认的操作）存进 SQLite（~/.mole-agent/checkpoints.db）。用原来的会话 id 再跑，
   SDK 会自动恢复上下文，这就是 /resume 的原理。SDK 默认的检查点只在内存里，重启就没了。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openjiuwen.harness.cli.storage import SessionStore, StoredMessage, StoredSession

from mole_agent.config import Settings

log = logging.getLogger("mole_agent")

TOOL_SUMMARY_CHARS = 200
TITLE_CHARS = 60
CHECKPOINT_DB = "checkpoints.db"


def sessions_dir(settings: Settings) -> Path:
    """当前项目的会话目录。目录名带项目名方便人看，带路径哈希区分同名项目。"""
    project = settings.project_dir.resolve()
    name = re.sub(r"[^\w.-]+", "_", project.name or "root")[:40]
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:8]
    return settings.home_dir / "sessions" / f"{name}-{digest}"


@dataclass
class SessionSummary:
    id: str
    model: str
    created_at: str          # ISO-8601（SDK 写的是 UTC）
    updated_at: float        # 文件修改时间，用来显示「最后活动」
    user_turns: int
    title: str               # 第一条用户输入
    path: Path


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()  # 与 SDK 的 SessionStore 一致：UTC ISO-8601


class HistoryRecorder:
    """把 REPL 会话写成 SDK SessionStore 格式的 JSON。任何写入失败都不影响对话本身。"""

    def __init__(self, settings: Settings) -> None:
        self.dir = sessions_dir(settings)
        self._session: Optional[StoredSession] = None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # 记下目录对应的项目路径，方便人工查找
            (self.dir / "project.txt").write_text(str(settings.project_dir.resolve()), encoding="utf-8")
        except OSError:
            pass

    @property
    def session_id(self) -> str:
        return self._session.session_id if self._session else ""

    def start(self, session_id: str, model: str) -> None:
        """开始记录一个新会话。第一条消息写入时才创建文件，空会话不会留下文件。"""
        self._session = StoredSession(session_id=session_id, model=model, created_at=_now())

    def resume(self, session: StoredSession) -> None:
        """续聊：接着往原来的会话文件里写。"""
        self._session = session

    def _add(self, role: str, content: str) -> None:
        if not content or self._session is None:
            return
        self._session.messages.append(StoredMessage(role=role, content=content, timestamp=_now()))
        try:
            self._save()
        except OSError:
            pass  # 磁盘满、权限问题等：历史记录是辅助功能，不能打断对话

    def _save(self) -> None:
        """和 SessionStore 写出的内容一样（asdict + indent=2），先写临时文件再替换，中途崩溃不会写坏原文件。"""
        assert self._session is not None
        path = self.dir / f"{self._session.session_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self._session), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def user(self, text: str) -> None:
        self._add("user", text)

    def assistant(self, text: str) -> None:
        self._add("assistant", text.strip())

    def system(self, text: str) -> None:
        self._add("system", text)

    def tool(self, name: str, args: str, ok: bool, decision: str, result: str) -> None:
        status = {"rejected_by_user": "用户拒绝", "rejected_by_guard": "被拦截"}.get(decision, "成功" if ok else "失败")
        # bash 结果第一行是回显的命令（Command: ...），和参数重复，取下一行
        first = next((line.strip() for line in result.splitlines()
                      if line.strip() and not line.startswith("Command:")), "")
        summary = f"{name}({args}) → {status}" + (f"：{first}" if first else "")
        self._add("tool", summary[:TOOL_SUMMARY_CHARS])


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("session_id") else None


def list_sessions(directory: Path, limit: Optional[int] = None) -> list[SessionSummary]:
    """列出目录里的会话，最新的在前。先用 SDK 的 list_sessions 拿到基本信息，再补标题和轮数。"""
    if not directory.is_dir():
        return []
    summaries: list[SessionSummary] = []
    for item in SessionStore(store_dir=directory).list_sessions():
        path = directory / f"{item['id']}.json"
        data = _read(path)
        if data is None:
            continue
        messages = [m for m in data.get("messages", []) if isinstance(m, dict)]
        users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
        title = " ".join(users[0].split()) if users else "（无输入）"
        summaries.append(SessionSummary(
            id=str(item["id"]),
            model=str(item.get("model", "")),
            created_at=str(item.get("created_at", "")),
            updated_at=path.stat().st_mtime,
            user_turns=len(users),
            title=title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…",
            path=path,
        ))
    summaries.sort(key=lambda s: (s.updated_at, s.created_at), reverse=True)
    return summaries[:limit] if limit else summaries


def load_session(directory: Path, session_id: str) -> Optional[StoredSession]:
    """按 SDK 的文件格式读出一个会话；找不到或文件损坏时返回 None。"""
    if not re.fullmatch(r"[\w.-]+", session_id):  # 防止 ../ 之类的路径
        return None
    data = _read(directory / f"{session_id}.json")
    if data is None:
        return None
    messages = [
        StoredMessage(
            role=str(m.get("role", "")),
            content=str(m.get("content", "")),
            timestamp=str(m.get("timestamp", "")),
            token_count=m.get("token_count"),
        )
        for m in data.get("messages", []) if isinstance(m, dict)
    ]
    return StoredSession(
        session_id=str(data["session_id"]),
        model=str(data.get("model", "")),
        created_at=str(data.get("created_at", "")),
        messages=messages,
    )


def local_time(value: str | float, fmt: str = "%m-%d %H:%M") -> str:
    """SDK 记录的是 UTC ISO 时间，显示时换成本地时间。"""
    try:
        moment = datetime.fromtimestamp(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value)
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime(fmt)
    except (ValueError, TypeError, OSError):
        return str(value)[:16]


# --------------------------------------------------------------------------- #
# 对话上下文的持久化（续聊）
# --------------------------------------------------------------------------- #
class ContextCheckpoint:
    """把 SDK 的默认检查点换成 SQLite 版的 PersistenceCheckpointer，退出时换回来并关闭数据库。

    数据库引擎由 Mole 自己创建（通过 conf 的 db_client 传给 SDK），因为 Runner.stop() 不会关闭它，
    不关的话 aiosqlite 的后台线程会让进程退不出去。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.checkpointer: Any = None
        self._engine: Any = None
        self._previous: Any = None

    async def open(self) -> None:
        import aiosqlite  # noqa: F401 —— 缺依赖时尽早报错，由调用方给出安装提示
        from openjiuwen.core.session.checkpointer import CheckpointerFactory
        from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
        from sqlalchemy import event
        from sqlalchemy.ext.asyncio import create_async_engine

        self.path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.path.as_posix()}", connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_wal(dbapi_conn: Any, _record: Any) -> None:  # 多个终端同时开 mole 时减少「database is locked」
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        try:
            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf={"db_client": engine})
            )
        except Exception:
            await engine.dispose()
            raise
        self._engine = engine
        self._previous = CheckpointerFactory.get_checkpointer()
        CheckpointerFactory.set_default_checkpointer(checkpointer)
        self.checkpointer = checkpointer

    async def exists(self, session_id: str) -> bool:
        if self.checkpointer is None:
            return False
        try:
            return bool(await self.checkpointer.session_exists(session_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("查询会话检查点失败：%s", exc)
            return False

    async def close(self) -> None:
        from openjiuwen.core.session.checkpointer import CheckpointerFactory

        if self._previous is not None:
            CheckpointerFactory.set_default_checkpointer(self._previous)
            self._previous = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self.checkpointer = None


async def open_context_checkpoint(settings: Settings) -> tuple[Optional[ContextCheckpoint], str]:
    """打开续聊用的检查点；失败时返回 (None, 原因)，调用方提示后照常使用（只是不能续聊）。"""
    checkpoint = ContextCheckpoint(settings.home_dir / CHECKPOINT_DB)
    try:
        await checkpoint.open()
    except ImportError as exc:
        return None, f"缺少依赖 {exc.name or exc}，续聊不可用。在虚拟环境里执行：pip install -e .（或 pip install aiosqlite）"
    except Exception as exc:  # noqa: BLE001
        return None, f"打开 {checkpoint.path} 失败，续聊不可用：{exc}"
    return checkpoint, ""
