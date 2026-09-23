import tempfile
from pathlib import Path

from mole_agent.cli import configure_sdk_logging
from mole_agent.config import Settings

# SDK 在首次打日志时按默认配置写 ./logs 并输出到控制台。
# 必须在收集测试模块（会 import openjiuwen）之前重定向，所以放在模块级执行。
configure_sdk_logging(Settings(home_dir=Path(tempfile.mkdtemp(prefix="mole-test-home-"))))
