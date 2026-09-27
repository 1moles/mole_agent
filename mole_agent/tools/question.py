"""question：执行过程中向用户提问（收集偏好、澄清模糊指令、在实现方案之间做决定）。

这个工具本身不干活：模型调用它时，rails.QuestionRail 先拦下来、中断等用户在终端里回答
（cli.Repl._answer_question），再把回答作为工具结果交回模型。所以：
- 不是 READ_ONLY：子 agent 在 task_tool 里运行，中断传不到终端，不能给它们；
- 常驻：卡片声明为 DIRECT，开启 SDK 的渐进式工具加载（tool_search）后也始终对模型可见。

本文件里的参数校验、选择解析、结果格式化都是纯函数，终端和 rail 共用，也方便单独测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from openjiuwen.core.foundation.tool import Tool, ToolExposure, tool

from mole_agent.config import Settings

NAME = "question"
CUSTOM_OPTION_LABEL = "自己输入答案"   # custom 打开时自动加在选项最后，对应描述里的 "Type your own answer"

DESCRIPTION = """Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- When `custom` is enabled (default), a "Type your own answer" option is added automatically; don't include "Other" or catch-all options
- Answers are returned as arrays of labels; set `multiple: true` to allow selecting more than one
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label"""

INPUT_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "description": "Questions to ask the user",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The complete question to ask"},
                    "header": {"type": "string", "description": "Very short label shown before the question (max 30 chars)"},
                    "options": {
                        "type": "array",
                        "description": "Available choices",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Display text (1-5 words, concise)"},
                                "description": {"type": "string", "description": "Explanation of the choice"},
                            },
                            "required": ["label"],
                        },
                    },
                    "multiple": {"type": "boolean", "default": False,
                                 "description": "Allow selecting more than one option"},
                    "custom": {"type": "boolean", "default": True,
                               "description": "Allow typing a custom answer (default: true)"},
                },
                "required": ["question", "header", "options"],
            },
        },
    },
    "required": ["questions"],
}


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
@dataclass
class Question:
    question: str
    header: str
    options: list[dict[str, str]]   # [{"label", "description"}]
    multiple: bool = False
    custom: bool = True

    @property
    def labels(self) -> list[str]:
        return [o["label"] for o in self.options]

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "header": self.header, "options": self.options,
                "multiple": self.multiple, "custom": self.custom}


def parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return dict(arguments) if isinstance(arguments, dict) else {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return default


def normalize_questions(arguments: Any) -> tuple[list[Question], Optional[str]]:
    """把模型传来的参数整理成 Question 列表，补上默认值。参数不合法时返回 ([], 给模型看的错误说明)。"""
    raw = parse_arguments(arguments).get("questions")
    if isinstance(raw, dict):   # 只问一个问题时，模型偶尔直接传对象
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "`questions` must be a non-empty array of {question, header, options}."
    questions: list[Question] = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return [], f"questions[{i}] must be an object with question, header and options."
        text = str(item.get("question") or "").strip()
        if not text:
            return [], f"questions[{i}].question is required."
        options: list[dict[str, str]] = []
        raw_options = item.get("options")
        if raw_options is None:
            raw_options = []
        if not isinstance(raw_options, list):
            return [], f"questions[{i}].options must be an array of {{label, description}}."
        for option in raw_options:
            if isinstance(option, str):
                option = {"label": option}
            label = str((option or {}).get("label") or "").strip() if isinstance(option, dict) else ""
            if not label:
                return [], f"questions[{i}].options: every option needs a non-empty label."
            if label not in (o["label"] for o in options):
                options.append({"label": label, "description": str(option.get("description") or "").strip()})
        custom = _bool(item.get("custom"), True)
        if not options and not custom:
            return [], f"questions[{i}] has no options and custom is false, so the user could not answer it."
        questions.append(Question(
            question=text,
            header=str(item.get("header") or "").strip(),
            options=options,
            multiple=_bool(item.get("multiple"), False),
            custom=custom,
        ))
    return questions, None


# --------------------------------------------------------------------------- #
# 终端输入解析
# --------------------------------------------------------------------------- #
@dataclass
class Selection:
    labels: list[str] = field(default_factory=list)   # 选中的选项
    want_custom: bool = False                          # 选了「自己输入答案」，还要再问一句
    text: str = ""                                     # 直接输入的自定义答案
    skip: bool = False                                 # 回车跳过
    error: str = ""                                    # 输入不合法，提示后重问


_SPLIT = re.compile(r"[\s,，、;；]+")
_RECOMMENDED = re.compile(r"\s*[（(](recommended|推荐)[)）]\s*$", re.IGNORECASE)


def _plain(label: str) -> str:
    return _RECOMMENDED.sub("", label).strip().lower()


def parse_selection(raw: str, question: Question) -> Selection:
    """解析用户对一个问题的输入：序号（多选用逗号或空格分隔）、选项原文，或者直接输入的自定义答案。"""
    text = raw.strip()
    if not text:
        return Selection(skip=True)
    if not question.options:   # 没有选项的开放问题：输入什么都是答案（包括纯数字）
        return Selection(text=text)
    count = len(question.options)
    custom_index = count + 1 if question.custom else None
    tokens = [t for t in _SPLIT.split(text) if t]
    if tokens and all(t.isdigit() for t in tokens):
        picked: list[int] = []
        for token in tokens:
            index = int(token)
            if not (1 <= index <= count or index == custom_index):
                upper = custom_index or count
                return Selection(error=f"序号要在 1-{upper} 之间")
            if index not in picked:
                picked.append(index)
        if len(picked) > 1 and not question.multiple:
            return Selection(error="这个问题只能选一个")
        return Selection(labels=[question.options[i - 1]["label"] for i in picked if i <= count],
                         want_custom=custom_index in picked)
    for option in question.options:   # 直接输入选项原文（不区分大小写，可省略「(Recommended)」）
        if _plain(option["label"]) == _plain(text):
            return Selection(labels=[option["label"]])
    if question.custom:
        return Selection(text=text)
    return Selection(error="请输入选项的序号" + ("（可以多选）" if question.multiple else ""))


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
def parse_answers(user_input: Any, questions: list[Question]) -> Optional[list[list[str]]]:
    """终端交回来的回答 → 每个问题一个标签数组；格式不对返回 None（rail 会重新问）。

    只认终端交回来的 {"answers": [...]}。续聊时用户新输入的一句话也会被 SDK 当成待回答问题的输入，
    那句话多半不是答案（比如「继续」），所以不当回答用，重新把问题问一遍。
    """
    if not isinstance(user_input, dict):
        return None
    answers = user_input.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        return None
    result: list[list[str]] = []
    for answer in answers:
        if isinstance(answer, str):
            answer = [answer] if answer.strip() else []
        if not isinstance(answer, list):
            return None
        result.append([str(a).strip() for a in answer if str(a).strip()])
    return result


def format_answers(questions: list[Question], answers: list[list[str]]) -> str:
    lines = ["User has answered your questions:"]
    for q, answer in zip(questions, answers):
        note = "" if answer else " (no answer, the user skipped this question)"
        lines.append(f"- {json.dumps(q.question, ensure_ascii=False)} = {json.dumps(answer, ensure_ascii=False)}{note}")
    lines.append("You can now continue with the user's answers in mind.")
    return "\n".join(lines)


def create(settings: Settings) -> Tool:
    async def question(questions: list) -> str:
        # 正常情况下 QuestionRail 会先拦下这个调用，走到这里说明没有终端可以问（例如 rail 没挂上）
        return "The question tool is not available here: there is no user to answer. Proceed with your best judgment."

    instance = tool(question, name=NAME, description=DESCRIPTION, input_params=INPUT_PARAMS)
    instance.card.exposure = ToolExposure.DIRECT   # 常驻：渐进式工具加载下也不隐藏
    instance.card.set_exposure_declared(True)
    return instance
