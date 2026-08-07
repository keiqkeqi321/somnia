"""工作区 .gitignore 规则匹配。

搜索类工具（grep/glob/tree/symbol 等）通过 :class:`GitignoreMatcher` 尊重工作区内
的 .gitignore 规则，遍历时直接剪枝被忽略的目录。匹配语义与 git 对齐（基于
pathspec 的 GitWildMatch 实现）：

- 嵌套 .gitignore 只作用于所在子树，深层规则优先于浅层；
- 同一文件内靠后的规则优先，``!`` 前缀取消忽略（pathspec 内部处理）；
- 父目录被忽略后无法通过 ``!`` 恢复其中的内容（与 git 一致，剪枝天然满足）。

边界：只读取锚点目录（一般为 workspace_root）及其内部的 .gitignore，不处理
父目录规则、.git/info/exclude 与全局 core.excludesFile。
"""

from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec

__all__ = ["GitignoreMatcher"]


class _GitignoreLayer:
    """单个目录下 .gitignore 的编译结果。"""

    __slots__ = ("base_dir", "spec", "lines", "line_numbers")

    def __init__(
        self,
        base_dir: Path,
        spec: GitIgnoreSpec,
        lines: list[str],
        line_numbers: list[int],
    ) -> None:
        self.base_dir = base_dir
        self.spec = spec
        self.lines = lines
        self.line_numbers = line_numbers


class GitignoreMatcher:
    """按需加载并缓存各目录 .gitignore 的匹配器。

    通过 :meth:`for_walk` 构造；``is_ignored`` / ``check`` 的路径必须位于
    锚点目录内部（遍历工具天然满足）。
    """

    def __init__(self, anchor_root: Path) -> None:
        self._anchor_root = anchor_root
        self._layers_cache: dict[Path, tuple[_GitignoreLayer, ...]] = {}

    @classmethod
    def for_walk(cls, workspace_root: Path, base_path: Path) -> "GitignoreMatcher":
        """为一次目录遍历构造匹配器。

        base_path 在工作区内时锚定 workspace_root（根级 .gitignore 依然生效）；
        在工作区外时锚定 base_path 自身（只应用其内部的嵌套规则）。
        """
        if base_path == workspace_root or base_path.is_relative_to(workspace_root):
            return cls(workspace_root)
        return cls(base_path)

    def _load_layer(self, dir_path: Path) -> _GitignoreLayer | None:
        gitignore_path = dir_path / ".gitignore"
        try:
            raw = gitignore_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        lines: list[str] = []
        line_numbers: list[int] = []
        for line_number, text in enumerate(raw.splitlines(), start=1):
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(text)
            line_numbers.append(line_number)
        if not lines:
            return None
        return _GitignoreLayer(dir_path, GitIgnoreSpec.from_lines(lines), lines, line_numbers)

    def _layers_for(self, dir_path: Path) -> tuple[_GitignoreLayer, ...]:
        """返回对 dir_path 生效的规则层，深层优先。"""
        cached = self._layers_cache.get(dir_path)
        if cached is not None:
            return cached
        if dir_path == self._anchor_root or not dir_path.is_relative_to(self._anchor_root):
            layers: tuple[_GitignoreLayer, ...] = ()
        else:
            layers = self._layers_for(dir_path.parent)
        own = self._load_layer(dir_path)
        if own is not None:
            layers = (own,) + layers
        self._layers_cache[dir_path] = layers
        return layers

    def check(self, path: Path, *, is_dir: bool) -> tuple[bool, tuple[str, int, str] | None]:
        """检查 path 是否被 .gitignore 忽略。

        Returns:
            ``(是否忽略, 决定性规则来源)``；来源为
            ``(.gitignore 标签, 行号, 规则文本)``，无匹配规则时为 None。
        """
        for layer in self._layers_for(path.parent):
            try:
                relative = path.relative_to(layer.base_dir).as_posix()
            except ValueError:
                continue
            if is_dir:
                # pathspec 要求目录路径带尾部斜杠才能命中 `dir/` 类模式
                relative += "/"
            result = layer.spec.check_file(relative)
            if result.include is None:
                continue
            source = None
            if result.index is not None and 0 <= result.index < len(layer.lines):
                source = (
                    self._layer_label(layer),
                    layer.line_numbers[result.index],
                    layer.lines[result.index].strip(),
                )
            return bool(result.include), source
        return False, None

    def is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        return self.check(path, is_dir=is_dir)[0]

    def _layer_label(self, layer: _GitignoreLayer) -> str:
        try:
            base = layer.base_dir.relative_to(self._anchor_root).as_posix()
        except ValueError:
            base = str(layer.base_dir)
        if base in ("", "."):
            return ".gitignore"
        return f"{base}/.gitignore"
