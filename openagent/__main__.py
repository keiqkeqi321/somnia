"""somnia 命令行入口模块.

这个模块允许通过 `python -m openagent` 或 `somnia` 运行。
"""

from openagent.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
