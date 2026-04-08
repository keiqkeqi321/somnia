"""somnia 命令行入口模块.

这个模块允许通过 `python -m open_somnia` 或 `somnia` 运行。
"""

from open_somnia.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
