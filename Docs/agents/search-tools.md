# Search Tools and Ignore Rules

Detailed reference for content/listing tools (`grep`, `glob`, `tree`,
`find_symbol`, and `read_file` auto-resolve). Kept out of `AGENTS.md` to keep
it lean; `AGENTS.md` carries only the short summary.

## Ignore Rules

Content/listing tools skip paths ignored by workspace `.gitignore` files
(nested files included, deepest rules win, `!` negation supported) via
`open_somnia/tools/gitignore.py::GitignoreMatcher`, in addition to the
hardcoded `EXPLORATION_IGNORED_DIR_NAMES` list. Ignored directories are pruned
during the walk. `grep` classifies files by extension before reading:
known-binary extensions (`BINARY_FILE_EXTENSIONS`) are skipped without opening,
known-text extensions (`TEXT_FILE_EXTENSIONS`) are read directly, and unknown
extensions fall back to NUL-byte sniffing of the first 8 KB. An explicit
single-file `path` always bypasses ignore rules. `list_ignored()`
(`open_somnia/tools/filesystem.py`) reports which paths are excluded and by
which rule; it is a diagnostic helper, intentionally not registered as an LLM
tool.

## grep acceleration via ripgrep

`grep` (`open_somnia/tools/filesystem.py::grep_search`) delegates to the
system `ripgrep` when available (`open_somnia/tools/ripgrep.py`), falling back
to the pure-Python implementation whenever rg is unsuitable. The Python path
is the source of correctness and is preserved verbatim as the fallback —
never delete it. Delegation conditions:

- **rg available** (`shutil.which("rg")` resolves and `--version` parses);
  disabled by `SOMNIA_NO_RG=1` (troubleshooting switch). `find_ripgrep()`
  caches the result per process.
- **pattern is pure ASCII** — rg encodes the pattern as UTF-8 bytes and cannot
  match GBK/GB18030-encoded files for CJK patterns; non-ASCII patterns go
  straight to Python (`_read_text_with_fallback` decodes gb18030/cp936).
- **ASCII pattern but a matched line carries non-UTF-8 bytes** (e.g. GBK file
  with Chinese content) — `run_ripgrep` decodes stdout with
  `errors='strict'` and returns `None` on `UnicodeDecodeError`, triggering the
  Python fallback so the Chinese text is rendered correctly.
- **rg exit code 2** (unsupported regex, e.g. backreferences `(?P=...)` /
  `\1`) — returns `None`, Python `re` handles it.
- **spawn failure / `base_path` outside workspace** — returns `None`, Python
  handles it.

argv mapping: `-H` (always print filename), `--null` (NUL-separated filename
defeats the Windows `D:\...` colon ambiguity), `-e <pattern>` (flag form
avoids PATTERN/PATH positional ambiguity), `-F` for literal / raw regex
passthrough, `-i` when case-insensitive, `--max-depth 1` when non-recursive,
`--sort path` for deterministic order, and `--no-require-git` so `.gitignore`
applies even without a `.git` directory. The hardcoded
`EXPLORATION_IGNORED_DIR_NAMES` / prefixes and `BINARY_FILE_EXTENSIONS` are
translated to `-g '!<name>/'` / `-g '!*.<ext>'` globs so projects without a
`.gitignore` still skip `.venv`/`node_modules`/binaries. `cwd` is set to
`base_path` so glob variants are evaluated relative to `base_path` (matching
the Python path's base-relative label matching); output paths are re-prefixed
to workspace-relative form on parse.

## find_symbol acceleration via ripgrep + scan budgets

`find_symbol` (`open_somnia/tools/filesystem.py::find_symbol`) delegates to
`run_ripgrep_symbols` in the same module when every query term is pure ASCII,
falling back to the pure-Python scan under the same conditions as `grep`
(no rg / `SOMNIA_NO_RG=1` / spawn failure / rg exit 2 / non-UTF-8 output).
The Python path stays the source of correctness.

Key differences from the grep frontend:

- rg only generates *candidate* lines: the union of all `SYMBOL_PATTERNS`
  regexes (multiple `-e`, deduped per line by rg). Symbol-name extraction,
  kind detection, and query-term filtering are re-checked in Python via the
  shared `_match_symbol_line` helper, so matching semantics are identical by
  construction. Query terms never reach rg, so case sensitivity semantics
  cannot drift.
- **Outside-workspace `base_path` is supported** (grep's frontend falls back
  there): the Python path labels outside results with absolute paths, so rg
  output is re-prefixed to the absolute form instead — this is the
  large-sibling-repo scan case.
- **No `--sort path`**: it disables rg's parallel traversal (~4.5× slower,
  measured). Results are collected as `(label, lineno, text)` tuples, sorted
  in Python, then `limit` is applied — deterministic output with parallel
  traversal. Collection is capped at 10 000 matches for pathological queries.
- Early stop (limit cap / budget) kills rg *before* `wait()`, otherwise a
  pipe-blocked rg trips the 5 s wait timeout and the partial results would be
  mistaken for a fallback signal.
- stderr goes to `DEVNULL` (permission-denied noise on messy trees can fill
  the pipe and deadlock rg); exit code + stdout carry all needed signals.

Scan budgets apply to both paths and are module constants in
`filesystem.py`: `SYMBOL_SCAN_MAX_FILE_BYTES` (2 MB, Python path stats before
reading — stat failure does *not* skip, preserving the old read-attempt
behavior; rg path uses `--max-filesize`) and
`SYMBOL_SCAN_TIME_BUDGET_SECONDS` (30 s wall clock). Exhausting the time
budget returns partial results with a `stopped early: time budget exceeded`
marker; an exhausted budget with zero hits never reports `(no matches)`,
since the scan did not complete.

