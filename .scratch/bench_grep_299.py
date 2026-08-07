# -*- coding: utf-8 -*-
"""对比三种搜索路径在 D:/Project/Git/299_March 上的耗时与结果一致性。

三种路径：
1. 系统 rg 直调（基线最快）；
2. somnia grep_search 间接触发 rg（本次改动后的默认路径）；
3. somnia grep_search 强制走 Python（``SOMNIA_NO_RG=1``，原实现兜底）。

预期：间接 rg 接近系统 rg，远快于 Python 兜底；三者结果一致（同样 4 条匹配、3 个文件）。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from open_somnia.tools import filesystem as fs
from open_somnia.tools import ripgrep as rg_module

TARGET = Path(r"D:/Project/Git/299_March")
PATTERN = "Only custom filters can be played"


class FakeSettings:
    workspace_root = TARGET

    class runtime:
        max_tool_output_chars = 60000


class FakeRuntime:
    settings = FakeSettings()


class FakeCtx:
    runtime = FakeRuntime()

    def raise_if_interrupted(self):
        return None


def bench_system_rg() -> tuple[float, str]:
    """直接调用系统 rg，使用与 somnia 相同的黑名单 globs（公平对比）。"""
    info = rg_module.find_ripgrep()
    argv = rg_module._build_ripgrep_argv(
        info=info,
        path_arg=".",
        pattern=PATTERN,
        glob_patterns=[],
        recursive=True,
        case_sensitive=False,
        use_regex=False,
    )
    t0 = time.perf_counter()
    proc = subprocess.run(argv, capture_output=True, cwd=str(TARGET))
    dt = time.perf_counter() - t0
    out = proc.stdout.decode("utf-8", errors="replace")
    return dt, out


def bench_somnia(use_rg: bool) -> tuple[float, str]:
    """somnia grep_search：use_rg=True 默认走 rg，use_rg=False 强制 Python 兜底。"""
    rg_module.reset_ripgrep_cache()
    # 通过环境变量控制是否走 rg（必须在 reset 缓存前设置）。
    if use_rg:
        os.environ.pop("SOMNIA_NO_RG", None)
    else:
        os.environ["SOMNIA_NO_RG"] = "1"
    rg_module.reset_ripgrep_cache()

    ctx = FakeCtx()
    t0 = time.perf_counter()
    result = fs.grep_search(ctx, {"pattern": PATTERN, "path": str(TARGET), "use_regex": False})
    dt = time.perf_counter() - t0
    return dt, result


def count_matches(text: str) -> int:
    """粗略统计匹配行数（系统 rg 用 NUL 分隔，somnia 已格式化为 path:lineno:text）。"""
    if not text or text == "(no matches)":
        return 0
    return sum(1 for line in text.splitlines() if line)


def main() -> None:
    print(f"target: {TARGET}")
    print(f"pattern: {PATTERN!r}")
    print()

    info = rg_module.find_ripgrep()
    print(f"[probe] ripgrep: {info.path if info else '<none>'} version={info.version if info else 'n/a'}")
    print()

    # 1. 系统 rg
    dt_sys, out_sys = bench_system_rg()
    n_sys = count_matches(out_sys)
    print(f"[1] system rg direct:        {dt_sys:6.2f}s  ({n_sys} match lines)")

    # 2. somnia 间接 rg
    dt_rg, out_rg = bench_somnia(use_rg=True)
    n_rg = count_matches(out_rg)
    print(f"[2] somnia grep (rg path):   {dt_rg:6.2f}s  ({n_rg} match lines)")

    # 3. somnia Python 兜底
    dt_py, out_py = bench_somnia(use_rg=False)
    n_py = count_matches(out_py)
    print(f"[3] somnia grep (python):    {dt_py:6.2f}s  ({n_py} match lines)")

    print()
    print("--- result consistency ---")
    print(f"[2] rg path result:\n{out_rg}")
    print()
    print(f"[3] python path result:\n{out_py}")
    print()
    if out_rg == out_py:
        print("[OK] rg path and python path produce IDENTICAL results")
    else:
        print("[DIFF] rg path and python path differ — inspect above")

    print()
    print("--- speedup summary ---")
    if dt_py > 0 and dt_rg > 0:
        print(f"indirect rg vs python:  {dt_py / dt_rg:.1f}x faster")
        print(f"indirect rg vs system:  {dt_rg / dt_sys:.2f}x (overhead of somnia wrapper)")


if __name__ == "__main__":
    main()
