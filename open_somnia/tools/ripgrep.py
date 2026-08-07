"""ripgrep 适配层：grep_search 的加速前端。

设计原则：
- rg 可用且匹配语义安全时，优先委托 rg（C 级遍历 + 内存映射，文件越多优势越大）；
- rg 不可用 / spawn 失败 / 不支持的 regex / 非 ASCII pattern / GBK 文件输出乱码时，
  调用方回退到纯 Python 实现；
- 输出格式与现有 Python 路径一致：``path:lineno:text``，``(no matches)`` 与截断标记不变。

回退是设计内的常态分支（``run_ripgrep`` 返回 ``None``），不是异常——Python 兜底实现是正确性的最终来源。

argv 构造要点（实测确认）：
- ``-H`` 强制始终输出文件名（否则单文件场景 rg 只输出 ``lineno:text``，丢失文件名）；
- ``--null`` 用 NUL 分隔文件名，规避 Windows 绝对路径 ``D:\\...`` 的冒号歧义；
- ``-e <pattern>`` 用 flag 形式传 pattern，避免 PATTERN/PATH 位置参数歧义；
- ``cwd=base_path`` + ``path=.`` → rg 输出相对 base_path 的路径，glob 也相对 base_path 评估，
  与 Python 路径（glob 匹配 base-relative 标签）语义对齐；解析时再补 base 相对 workspace 的前缀；
- ``--sort path`` 保证跨文件顺序确定（结果可复现，测试可断言）。

GBK parity（实测确认）：
- ASCII pattern 在 GBK/GB18030 文件里能搜到（rg 按字节匹配），但输出是原始 GBK 字节；
- 用 ``errors='strict'`` 解码 rg stdout，遇非 UTF-8 字节（GBK 内容）解码失败 → 返回 None 回退 Python，
  Python 路径用 gb18030/cp936 解码链正确还原中文。这样既享受 rg 加速，又保住 GBK 输出正确性。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# filesystem 在被 grep_search 触发时才反向 import 本模块；
# 这里若顶层 import filesystem 会形成循环导入，因此常量与中断检查改为函数内延迟 import。
from open_somnia.runtime.interrupts import TurnInterrupted


# 进程级缓存：避免每次 grep 都 spawn 一次 ``rg --version``。
_rigrep_cache: Any | None = None
# 哨兵：区分"已探测且无 rg"与"尚未探测"。
_RIPEGREP_NOT_PROBED: Any = object()


class RipgrepInfo:
    """探测到的 rg 可执行文件信息。"""

    __slots__ = ("path", "version", "major_version")

    def __init__(self, path: str, version: str, major_version: int) -> None:
        self.path = path
        self.version = version
        self.major_version = major_version


def find_ripgrep() -> RipgrepInfo | None:
    """查找系统中的 rg，解析版本号，结果进程级缓存。

    环境变量 ``SOMNIA_NO_RG=1`` 强制禁用 rg，用作排查开关（无论系统是否装了 rg 都回退 Python）。
    """
    global _rigrep_cache
    if _rigrep_cache is None:
        _rigrep_cache = _probe_ripgrep()
    cached = _rigrep_cache
    if cached is _RIPEGREP_NOT_PROBED:
        return None
    return cached


def _probe_ripgrep() -> Any:
    if os.environ.get("SOMNIA_NO_RG") == "1":
        return _RIPEGREP_NOT_PROBED

    executable = shutil.which("rg")
    if executable is None:
        return _RIPEGREP_NOT_PROBED

    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5.0,
            text=False,
        )
    except (OSError, subprocess.SubprocessError):
        # 探测失败按"无 rg"处理，让回退兜底。
        return _RIPEGREP_NOT_PROBED

    output = proc.stdout.decode("utf-8", errors="replace")
    version, major_version = _parse_ripgrep_version(output)
    if version is None:
        # 无法解析版本号也视为不可用——避免盲目传不支持的 flag。
        return _RIPEGREP_NOT_PROBED
    return RipgrepInfo(path=executable, version=version, major_version=major_version)


def _parse_ripgrep_version(output: str) -> tuple[str | None, int]:
    """从 ``rg --version`` 输出解析版本号与主版本号。

    典型输出首行：``ripgrep 13.0.0 (rev af6b6c543b)``。
    """
    match = re.search(r"ripgrep\s+(\d+)(?:\.(\d+))?(?:\.(\d+))?", output)
    if match is None:
        return None, 0
    version = match.group(0).split(None, 1)[1]
    major_version = int(match.group(1))
    return version, major_version


def reset_ripgrep_cache() -> None:
    """清空进程级缓存（测试用：让 ``find_ripgrep`` 重新探测）。"""
    global _rigrep_cache
    _rigrep_cache = None


def run_ripgrep(
    ctx: Any,
    *,
    workspace_root: Path,
    base_path: Path,
    pattern: str,
    glob_patterns: list[str],
    recursive: bool,
    case_sensitive: bool,
    use_regex: bool,
    limit: int,
    max_output_chars: int,
) -> str | None:
    """委托 rg 执行搜索；返回格式化结果字符串，或 ``None`` 触发 Python 回退。

    返回 ``None`` 的情形：
    - 系统无 rg / ``SOMNIA_NO_RG=1``；
    - spawn 失败（``FileNotFoundError`` / ``OSError``）；
    - rg 退出码 = 2（regex 不支持，如 backreference）；
    - rg stdout 含非 UTF-8 字节（GBK 文件的中文内容）→ 回退 Python（gb18030/cp936 解码链）；
    - base_path 在工作空间外（allow_outside=True）→ 无法构造 workspace 相对前缀。

    调用方（grep_search）在 pattern 非 ASCII 时已直接走 Python，因此本函数可假设 pattern 为纯 ASCII。
    """
    info = find_ripgrep()
    if info is None:
        return None

    path_arg, cwd, base_prefix, single_file = _resolve_path_cwd_prefix(workspace_root, base_path)
    if path_arg is None:
        # base_path 在工作空间外（allow_outside=True）→ 无法构造 workspace 相对前缀，回退 Python。
        return None

    argv = _build_ripgrep_argv(
        info=info,
        path_arg=path_arg,
        pattern=pattern,
        glob_patterns=glob_patterns,
        recursive=recursive,
        case_sensitive=case_sensitive,
        use_regex=use_regex,
    )

    # 延迟 import：filesystem 在被 grep_search 触发时才 import 本模块，避免顶层循环导入。
    from open_somnia.tools.filesystem import _raise_if_tool_interrupted

    matches: list[str] = []
    truncated = False
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
    except (OSError, ValueError):
        # spawn 失败（rg 被删、权限问题、cwd 无效）→ 回退。
        return None

    try:
        assert proc.stdout is not None
        # 逐行读取原始字节（按 \n 切分，ASCII 安全，不破坏多字节字符）。
        # strict UTF-8 解码：遇 GBK 文件的中文内容解码失败 → 整体回退 Python。
        for raw_line in proc.stdout:
            _raise_if_tool_interrupted(ctx)
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                # rg 输出了非 UTF-8 字节（典型：GBK 文件的中文行）→ 回退 Python 还原正确文本。
                return None
            if not line:
                continue
            formatted = _parse_ripgrep_line(line, base_prefix, single_file=single_file)
            if formatted is None:
                # ``binary file matches...`` 等通知行不符合 path\0lineno:text 结构，自然丢弃。
                continue
            matches.append(formatted)
            if len(matches) >= limit:
                truncated = True
                break
        returncode = proc.wait(timeout=5.0)
    except TurnInterrupted:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        # 终止进程并关闭管道，避免 ResourceWarning（哪怕正常返回也要清理）。
        if proc.poll() is None:
            proc.kill()
        _close_proc_streams(proc)

    # exit 2 表示 rg 出错（典型：不支持的 regex，如 backreference）→ 回退 Python re。
    if returncode == 2:
        return None

    if not matches:
        return "(no matches)"
    if truncated:
        matches.append(f"... ({limit} matches shown)")
    return "\n".join(matches)[:max_output_chars]


def _close_proc_streams(proc: subprocess.Popen[Any]) -> None:
    """关闭 rg 进程的 stdout/stderr 管道，避免 ResourceWarning。

    正常返回、回退、中断三条路径都经过 finally 调用本函数。``proc.kill()`` 后再 wait 收尸，
    确保进程不残留为僵尸。
    """
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except Exception:
        pass
    try:
        if proc.stderr is not None:
            proc.stderr.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def _resolve_path_cwd_prefix(
    workspace_root: Path,
    base_path: Path,
) -> tuple[str | None, str | None, str, bool]:
    """构造 rg 的 path 参数、cwd，以及解析输出时需要补的 workspace 相对前缀。

    策略：cwd 设为 base_path（目录）或 base_path 的父目录（单文件），path 参数用 ``.`` 或文件名。
    这样 rg 输出的文件路径相对 base_path，glob 也相对 base_path 评估，与 Python 路径
    （glob 匹配 base-relative 标签）语义对齐。

    base_prefix 是 base_path 相对 workspace_root 的 posix 路径（``frontend/src``、``.`` 等），
    解析 rg 输出时用它把 base-relative 路径补回 workspace 相对标签。
    single_file 标记 base_path 是否为单文件（单文件时 base_prefix 已是完整 workspace 相对路径，
    解析时不再追加 rg 输出的文件名）。

    base_path 在工作空间外（allow_outside=True）时返回 ``(None, None, "", False)``，由调用方回退 Python。
    """
    try:
        relative = base_path.relative_to(workspace_root)
    except ValueError:
        return None, None, "", False
    base_prefix = relative.as_posix() or "."

    if base_path.is_dir():
        return ".", str(base_path), base_prefix, False
    # 单文件：cwd 设为其父目录，path 参数用文件名（rg 对显式文件路径绕过 ignore 规则，与现状一致）。
    return base_path.name, str(base_path.parent), base_prefix, True


def _build_ripgrep_argv(
    *,
    info: RipgrepInfo,
    path_arg: str,
    pattern: str,
    glob_patterns: list[str],
    recursive: bool,
    case_sensitive: bool,
    use_regex: bool,
) -> list[str]:
    """把 somnia grep 参数映射成 rg argv。

    关键 parity 点：
    - ``-H`` 强制输出文件名（单文件场景否则丢文件名）；
    - ``--null`` 用 NUL 分隔文件名，规避 Windows 绝对路径 ``D:\\...`` 的冒号歧义；
    - ``--sort path`` 保证跨文件顺序确定（结果可复现，测试可断言）；
    - 内置目录黑名单与扩展名黑名单转成 ``-g '!name/'`` / ``-g '!*.ext'``，确保无 .gitignore 的项目里
      ``.venv``/``node_modules`` 等仍被排除（现有测试会强制验证这一点）；
    - 多个 ``-g`` 是 OR，brace 原生支持。
    """
    # 延迟 import：filesystem 的常量在 grep_search 触发时已加载完成。
    from open_somnia.tools.filesystem import (
        BINARY_FILE_EXTENSIONS,
        EXPLORATION_IGNORED_DIR_NAMES,
        EXPLORATION_IGNORED_DIR_PREFIXES,
    )

    argv: list[str] = [
        info.path,
        "-H",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--null",
        "--sort",
        "path",
        # ``--no-require-git`` 让 rg 在无 .git 目录时仍遵守 .gitignore（rg 12.0.0 起支持）。
        # 低于 12 的 rg 会 exit 2，由 run_ripgrep 回退 Python（仍正确处理 .gitignore）。
        "--no-require-git",
    ]

    # pattern：字面量用 -F；regex 原样传（不支持的正则 exit 2 → 回退 Python re）。
    # 用 -e 以 flag 形式附加，避免 PATTERN/PATH 位置参数歧义。
    if use_regex:
        argv.extend(["-e", pattern])
    else:
        argv.extend(["-F", "-e", pattern])

    if not case_sensitive:
        argv.append("-i")
    if not recursive:
        argv.append("--max-depth")
        argv.append("1")

    # glob：每个 variant 一个 -g（含 brace 展开）。
    for variant in glob_patterns:
        if variant and variant != "*":
            argv.extend(["-g", variant])

    # 内置目录黑名单：精确名字 → ``-g '!<name>/'``。
    for name in sorted(EXPLORATION_IGNORED_DIR_NAMES):
        argv.extend(["-g", f"!{name}/"])
    # 内置目录前缀（如 ``.tmp``、``tmp``）→ ``-g '!<prefix>*/'``。
    for prefix in EXPLORATION_IGNORED_DIR_PREFIXES:
        argv.extend(["-g", f"!{prefix}*/"])
    # 二进制扩展名黑名单：确定性跳过，连 rg 的二进制探测都省掉。
    for ext in sorted(BINARY_FILE_EXTENSIONS):
        argv.extend(["-g", f"!*{ext}"])

    # 路径：``.``（目录）或文件名（单文件），配合 cwd=base_path。
    argv.append(path_arg)
    return argv


def _parse_ripgrep_line(line: str, base_prefix: str, *, single_file: bool = False) -> str | None:
    """解析一行 rg 输出（``path\0lineno:text``）为 ``relative:lineno:text``。

    - 用 NUL 分隔文件名，规避 Windows 路径 ``D:\\...`` 的冒号歧义；
    - 剩余部分按第一个冒号拆 lineno 与 text；
    - 解析失败的行返回 ``None``（如 ``binary file matches...`` 通知行）。

    rg 在 cwd=base_path 下输出形如 ``.\\src\\app.py`` / ``app.py`` / ``.\\README.md``，
    先归一化为 base-relative posix 路径，再用 base_prefix 补回 workspace 相对标签
    （与 filesystem._relative_label 输出 ``src/app.py`` 对齐）。

    single_file=True 时 base_prefix 已是完整 workspace 相对路径，直接使用，不追加 rg 输出的文件名。
    """
    nul_index = line.find("\x00")
    if nul_index < 0:
        return None
    file_path_text = line[:nul_index]
    rest = line[nul_index + 1 :]
    colon_index = rest.find(":")
    if colon_index < 0:
        return None
    lineno_text = rest[:colon_index]
    if not lineno_text.isdigit():
        return None
    content = rest[colon_index + 1 :]

    if single_file:
        # 单文件：base_prefix 即完整 workspace 相对路径（如 ``src/app.py``），rg 输出的文件名重复，忽略。
        relative = base_prefix
    else:
        relative = _normalize_relative_label(file_path_text, base_prefix)
    return f"{relative}:{lineno_text}:{content}"


def _normalize_relative_label(file_path_text: str, base_prefix: str) -> str:
    """把 rg 输出的文件路径归一化为 workspace 相对标签。

    rg 输出相对 base_path 的路径（``.\\src\\app.py``、``app.py`` 等），先剥前导 ``.\\``/``./``、
    反斜杠转正斜杠得到 base-relative posix 路径，再用 base_prefix 拼成 workspace 相对标签。
    base_prefix 为 ``.`` 时（base_path 即 workspace_root）不加前缀。
    """
    text = file_path_text
    # 剥离前导 ``.\`` 或 ``./``（rg 用 ``.`` 表示 cwd 时会加这个前缀）。
    while text.startswith("./") or text.startswith(".\\"):
        text = text[2:]
    # 反斜杠转正斜杠（Windows 路径分隔符归一）。
    text = text.replace("\\", "/")
    if not text:
        return base_prefix
    if base_prefix and base_prefix != ".":
        return f"{base_prefix}/{text}"
    return text
