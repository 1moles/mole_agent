"""question 工具：参数校验、终端输入解析、中断-回答-继续的完整流程（假模型端到端）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.runner import Runner

from mole_agent import cli
from mole_agent.agent import build_agent
from mole_agent.config import Settings
from mole_agent.tools import build_custom_tools
from mole_agent.tools.question import (
    DESCRIPTION, Question, create, format_answers, normalize_questions, parse_answers, parse_selection,
)

from test_e2e_fake_llm import ScriptedModel

DB = {"question": "用哪个数据库？", "header": "数据库", "options": [
    {"label": "PostgreSQL (Recommended)", "description": "功能全"}, {"label": "SQLite", "description": "零配置"}]}
FEATURES = {"question": "要哪些功能？", "header": "功能", "multiple": True, "custom": False,
            "options": [{"label": "登录"}, {"label": "日志"}, {"label": "缓存"}]}
NAME = {"question": "项目叫什么？", "header": "名字", "options": []}


# ---------------------------------------------------------------- 工具定义
def test_tool_card_is_resident_with_given_description(tmp_path: Path):
    card = create(Settings(project_dir=tmp_path, home_dir=tmp_path / "home")).card
    assert card.name == "question" and card.description == DESCRIPTION
    assert card.exposure == ToolExposure.DIRECT and card.get_exposure_declared() is True   # 常驻，不会被 tool_search 藏起来
    item = card.input_params["properties"]["questions"]["items"]
    assert card.input_params["required"] == ["questions"]
    assert item["required"] == ["question", "header", "options"]
    assert item["properties"]["custom"]["default"] is True and item["properties"]["multiple"]["default"] is False


def test_question_tool_is_not_given_to_read_only_subagents(tmp_path: Path):
    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / "home")
    assert "question" in {t.card.name for t in build_custom_tools(settings)}
    assert "question" not in {t.card.name for t in build_custom_tools(settings, read_only=True)}


# ---------------------------------------------------------------- 参数校验
def test_normalize_fills_defaults():
    questions, error = normalize_questions(json.dumps({"questions": [DB, FEATURES, NAME]}))
    assert error is None
    db, features, name = questions
    assert (db.multiple, db.custom, features.multiple, features.custom) == (False, True, True, False)
    assert db.labels == ["PostgreSQL (Recommended)", "SQLite"] and name.options == []
    single, _ = normalize_questions({"questions": {"question": "单个对象也行", "header": "h", "options": ["甲", "乙"]}})
    assert single[0].labels == ["甲", "乙"]


@pytest.mark.parametrize("args, message", [
    ({}, "non-empty array"),
    ({"questions": []}, "non-empty array"),
    ({"questions": ["不是对象"]}, "must be an object"),
    ({"questions": [{"header": "h", "options": []}]}, "question is required"),
    ({"questions": [{"question": "q", "header": "h", "options": [{"description": "没有 label"}]}]}, "non-empty label"),
    ({"questions": [{"question": "q", "header": "h", "options": [], "custom": False}]}, "could not answer"),
])
def test_normalize_rejects_bad_arguments(args, message):
    questions, error = normalize_questions(args)
    assert questions == [] and message in error


# ---------------------------------------------------------------- 终端输入
def _q(**overrides) -> Question:
    return normalize_questions({"questions": [{**DB, **overrides}]})[0][0]


def test_parse_single_select():
    q = _q()
    assert parse_selection("1", q).labels == ["PostgreSQL (Recommended)"]
    assert parse_selection(" 2 ", q).labels == ["SQLite"]
    assert parse_selection("sqlite", q).labels == ["SQLite"]                         # 直接输入选项原文
    assert parse_selection("postgresql", q).labels == ["PostgreSQL (Recommended)"]   # 可省略 (Recommended)
    assert parse_selection("3", q).want_custom                                         # 最后一项：自己输入答案
    assert parse_selection("用 MySQL", q).text == "用 MySQL"
    assert parse_selection("", q).skip
    assert "只能选一个" in parse_selection("1,2", q).error
    assert "1-3" in parse_selection("4", q).error


def test_parse_multi_select_and_no_custom():
    q = normalize_questions({"questions": [FEATURES]})[0][0]
    assert parse_selection("1，3", q).labels == ["登录", "缓存"]
    assert parse_selection("3 1 3", q).labels == ["缓存", "登录"]      # 去重，保持输入顺序
    assert parse_selection("1、2、3", q).labels == ["登录", "日志", "缓存"]
    assert "1-3" in parse_selection("4", q).error                          # 没有「自己输入答案」这一项
    assert "序号" in parse_selection("随便写点", q).error                  # 不允许自定义答案


def test_open_question_takes_any_text():
    q = normalize_questions({"questions": [NAME]})[0][0]
    assert parse_selection("2024", q).text == "2024"


def test_answers_and_result_format():
    questions, _ = normalize_questions({"questions": [DB, FEATURES]})
    assert parse_answers({"answers": [["SQLite"], []]}, questions) == [["SQLite"], []]
    assert parse_answers({"answers": [["SQLite"]]}, questions) is None           # 个数不对
    assert parse_answers({"answers": "x"}, questions) is None
    assert parse_answers("继续", questions[:1]) is None                          # 续聊时新输入的话不当回答
    text = format_answers(questions, [["SQLite"], []])
    assert '"用哪个数据库？" = ["SQLite"]' in text and '"要哪些功能？" = [] (no answer' in text
    assert text.startswith("User has answered your questions:")


# ---------------------------------------------------------------- 端到端
@pytest.fixture()
async def env(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    settings = Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                        project_dir=proj, home_dir=tmp_path / "home")
    bundle = build_agent(settings)
    fake = ScriptedModel()
    seen: list[list] = []
    stream = fake.stream

    async def recording_stream(*args, messages=None, **kwargs):
        seen.append(list(messages or []))
        async for chunk in stream(*args, messages=messages, **kwargs):
            yield chunk

    model = bundle.agent.deep_config.model
    model.stream, model.invoke = recording_stream, fake.invoke
    repl = cli.Repl(settings, bundle)
    answers: list[str] = []
    asked: list[str] = []

    async def fake_ask(message: str) -> str:
        asked.append(message)
        return answers.pop(0) if answers else ""

    repl._ask = fake_ask
    await Runner.start()
    try:
        yield repl, fake, seen, answers, asked
    finally:
        await Runner.stop()


def _tool_results(messages: list) -> list[str]:
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        if role == "tool":
            out.append(str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")))
    return out


async def test_ask_and_continue(env, capsys):
    repl, fake, seen, answers, asked = env
    fake.plan(("", [("question", {"questions": [DB, FEATURES, NAME]})]), ("好的，按你的选择来。", []))
    # 数据库：先选「自己输入答案」又没填 → 重问 → 直接输入；功能：多选；名字：开放问题
    answers[:] = ["3", "", "我的数据库", "1，3", "mole-demo"]
    text = await repl.run_turn("帮我起个项目")

    assert "按你的选择来" in text
    assert len(asked) == 5 and "你的答案" in asked[1]
    result = _tool_results(seen[-1])[-1]                              # 回答作为工具结果交回模型
    assert '"用哪个数据库？" = ["我的数据库"]' in result
    assert '"要哪些功能？" = ["登录", "缓存"]' in result
    assert '"项目叫什么？" = ["mole-demo"]' in result

    out = capsys.readouterr().out
    assert "需要你的输入（共 3 个问题）" in out and "[数据库] 用哪个数据库？" in out and "（可多选）" in out
    assert "[名字] 项目叫什么？ （直接输入回答）" in out
    assert "3. 自己输入答案" in out and "4. 自己输入答案" not in out   # 功能题 custom=false，不加这一项
    assert "● question(数据库、功能、名字)" in out
    assert '"要哪些功能？" = ["登录", "缓存"]' in out                  # 回答逐条显示在工具结果里


async def test_invalid_input_is_asked_again(env, capsys):
    repl, fake, seen, answers, asked = env
    fake.plan(("", [("question", {"questions": [DB]})]), ("好", []))
    answers[:] = ["1,2", "9", "2"]
    await repl.run_turn("选个数据库")
    out = capsys.readouterr().out
    assert "只能选一个" in out and "序号要在 1-3 之间" in out
    assert '"用哪个数据库？" = ["SQLite"]' in _tool_results(seen[-1])[-1]


async def test_skip_returns_empty_answer(env):
    repl, fake, seen, answers, asked = env
    fake.plan(("", [("question", {"questions": [DB]})]), ("那我自己定了", []))
    answers[:] = [""]
    await repl.run_turn("选个数据库")
    assert '= [] (no answer, the user skipped this question)' in _tool_results(seen[-1])[-1]


async def test_bad_arguments_go_back_to_model_without_asking(env):
    repl, fake, seen, answers, asked = env
    fake.plan(("", [("question", {"questions": []})]), ("我换个问法", []))
    await repl.run_turn("随便问问")
    assert asked == []
    assert "Error: `questions` must be a non-empty array" in _tool_results(seen[-1])[-1]


async def test_pending_question_is_asked_again_after_resume(tmp_path: Path):
    """在回答问题时按了 Ctrl+C（或者关了终端）：续聊后先把问题重新问一遍。"""
    pytest.importorskip("aiosqlite")
    from test_resume import MoleProcess

    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    async with MoleProcess(proj, home) as first:
        async def cancel(_message: str) -> str:
            raise asyncio.CancelledError

        first.repl._ask = cancel
        first.fake.plan(("", [("question", {"questions": [DB]})]))
        await first.repl.run_interruptible("选个数据库")
        sid = first.repl.session_id

    async with MoleProcess(proj, home, answers=["2"]) as second:
        assert await second.repl.resume(sid)
        second.fake.plan(("用 SQLite。", []))
        await second.repl.run_interruptible("继续")
        assert any("输入序号" in q for q in second.asked)            # 重新问了，「继续」没有被当成回答
        history_dir = second.repl.history.dir
    from mole_agent.history import load_session

    tools = [m.content for m in load_session(history_dir, sid).messages if m.role == "tool"]
    assert tools == ['question(数据库) → 成功："用哪个数据库？" = ["SQLite"]']   # 历史里记下了回答
