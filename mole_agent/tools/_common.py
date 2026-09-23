"""工具共用的小函数。只放至少两个工具会用到、或每个新工具都该用的东西。"""

from __future__ import annotations

from pathlib import Path


def resolve_inside(root: Path, rel: str | None) -> Path:
    """把相对路径解析到项目内；越界直接报错（与 SDK 的文件沙箱语义保持一致）。"""
    root = root.resolve()
    target = (root / (rel or ".")).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界：{rel!r} 不在项目目录 {root} 内")
    return target


def truncate(text: str, limit: int, hint: str = "") -> str:
    """输出超过 limit 个字符时截断，并告诉模型被截断了、可以怎么看完整内容。"""
    if len(text) <= limit:
        return text
    tail = f"\n...(输出过长，已截断到 {limit} 字符{'；' + hint if hint else ''})"
    return text[:limit] + tail
