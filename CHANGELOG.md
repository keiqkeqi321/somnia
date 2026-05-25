# Changelog

## 0.5.3 (2026-05-25)

- ignore (480343d)
- Add dedicated vision provider configuration (ce04058)
- Allow ruff format shell commands (593e09b)
- Skip noisy dirs in filesystem searches (02d80c8)
- indexFile (2969b96)
- Add SSE MCP transport support (4be9ee5)
- fix: narrow grep glob candidate scanning (8aecc47)
- Windows 绝对路径在 workspace 内安全校验 (cb46a9b)
- Show inline tool images in desktop (8dbdaac)
- Allow collapsing active desktop project (e470261)
- Limit desktop project list (b95a597)
- Restore last desktop session (b522279)
- Improve desktop image preview zoom (ed8e94c)
- Show MCP screenshot results in desktop (be83c1b)
- Fix stdio MCP framing for Playwright (ca05814)
- Preserve GitNexus block during init (8316383)
- index file (fdfdf18)
- Ignore Somnia workspace state by default (b908f2d)
- Allow init command extra instructions (5c60056)
- Add init command for project instructions (685253d)
- Load project instruction files into prompt (3b263ea)
- Support Claude skill directories (1bdc088)
- desktop window tools (b2fb0be)
- Add desktop i18n framework and titlebar icon (8b56706)
- 本地化 (98ae44d)
- 归档线程批量管理 (8f53899)
- Add desktop context commands (b143fdc)
- feat(desktop): auto-scroll to bottom when switching sessions (46e5d47)
- Add structured file diff hunks (af3b419)
- code diff show (51d252b)
- Improve tool call result cards (1611f95)
- Fix project connection placeholder dedupe (ce2765b)
- Add tool call flood guard (88c72a8)
- Fix managed sidecar shutdown on desktop (3445d79)
- 安装环境预检 (6dd780c)
- 打包脚本 (f23d120)
- Hide desktop console in release builds (94f0cac)
- 更换应用图标 (4e839db)
- Add desktop MCP server management (a1a00bf)
- Route desktop config commands to UI panels (6e0b4b3)
- Enforce task dependency blocking (cd1f8dd)
- Parallel task scheduling (b760ef8)
- Add desktop task graph activity view (7724a92)
- Add built-in Somnia configuration skill (c990bfe)
- docs (508a1d1)
- setting s (93f1da3)
- Add scoped desktop configuration settings (e69d41b)
- Scope team archives by session (11c820c)
- Scope task tools by session (facd80e)
- Scope team inbox messages by session (1f38dcf)
- Isolate teammates by session (8711f6e)
- Fix teammate inbox wait and idle restore (ffcb3d5)
- Show subagent activity in TUI (fd49b24)
- Support MCP image result metadata (eb6c774)
- ppService/REPL 事件消费路径：assistant_delta 已经先到，但ConsoleStreamer 会缓存没有换行的 markdown 文本；随后工具结果直接打印，导致文本最终在工具后面才 flush。 (f1bd3ad)
- docs: 补充桌面端源码安装命令和调试启动命令，新增 dev-desktop.ps1 一键开发脚本 (0482fcc)
- Fix live tool text ordering (d8e21ad)
- Speed up path mention completion (befbc68)
- Add concise problem solving workflow guidance (80e4f1a)
- Fix persistent TUI status panel layout (8e6d3ce)
- 同步优化desktop  启动速度 (bc13c86)
- Speed up resume session picker (bb6cbe8)
- Add Mermaid diagram viewer to desktop UI (7feac4e)
- Shorten desktop session dates after one day (bfb191d)
- Refine session card status affordances (85cdd2a)
- Add desktop settings view and archived session management (16cd4e3)
- Fix desktop image prompt delivery (55ceb77)
- fix(desktop-ui): render markdown tables in chat (9a2f5af)
- Add custom desktop titlebar controls (3d70797)
- Add desktop path mention suggestions (865e970)
- Tools used show (64bce36)
- Merge assistant conversation bubbles (6dc1954)
- Show desktop tool events in conversation stream (190465b)
- Add desktop prompt queue loop injection (fd98833)
- Show desktop decisions inline (02c0d07)
- Allow two concurrent desktop sessions (f10ba8f)
- Improve desktop session   layout (2fd8ea5)
- 存档逻辑回滚 (be89658)
- Persist desktop projects and   improve composer input (d4cb896)
- fix: harden desktop release   checks (874203a)
- feat(desktop): render   markdown messages (8e8abd7)
- todo show (2c2c95f)
- 发版脚本修改 (4216406)

## 0.5.2 (2026-04-26)

- Improve desktop chat send feedback (d3dc5bd)
- Refine desktop chat interactions (07a7009)
- Support multi-project desktop sessions (f8f9eca)
- Fix retired macOS GitHub Actions runner labels (dc21338)

## 0.5.1 (2026-04-26)

- 妗岄潰绔疌D 鍜?鏂囨。 (6e3957e)
- 鑷姩閰嶇疆CI 鏋勫缓鐜 (30848f8)
- feat(desktop): bootstrap local Windows release toolchain (f903967)
- 褰掓。desktop app (40163a7)
- feat(desktop): add installer and distribution pipeline (a84a4ec)
- feat(desktop): add acceptance launcher and Tauri assets (0e6751c)
- 鍒濆鍖栦簡涓€涓熀浜?Tauri 鐨勬闈?UI 瀹㈡埛绔紝骞剁浉搴旀墿灞曚簡 Python 鍚庣 API锛堣繍琛屾椂鐘舵€併€佸伐鍏锋棩蹇楁煡璇€佹墽琛屾ā寮忎俊鎭級 (f72854c)
- feat(sidecar): add desktop backend sidecar service (054d298)
- feat(cli): adapt CLI to app service for phase 2 (f93f262)
- feat: add phase 1 app service layer (21c3da1)

## 0.5.0 (2026-04-24)

- Unify persisted image references across history (da8f075)
- feat(cli): add clipboard image paste support (3bfd0ba)
- Improve interrupt responsiveness for tool execution (5c505ec)
- Add multimodal image reading support (c0ba6fe)
- Handle multiline paste as one REPL input (30a3e69)

## 0.4.9 (2026-04-22)

- Fix Windows CJK prompt cursor drift (030b199)
- Improve todo reconciliation and single-file symbol search (2df2abb)
- Add configurable reasoning levels and auto mode (1da6763)
- Echo next-loop queued prompts in REPL (06df3fd)
- Refine queued Esc handling at loop boundaries (0b873bb)
- Shorten queued prompt notice in REPL (275444f)
- Classify edit misses as content_not_found (81c858e)
- Support brace-expanded glob patterns in filesystem search (f034577)
- Return explicit open-todo stop status at max rounds (2fbf4a3)
- Document persisted read_file overlap state (eb476aa)
- 统一化工具错误，自动纠正 (8754845)

## 0.4.8 (2026-04-20)

- release: v0.4.7 (ad8dbbe)
- Persist read_file overlap coverage state (351a081)
- Add scoped read_file overlap pruning (0874ef7)
- Add ranged read_file support and payload dedupe (1aed30b)
- Document TodoWrite reminder behavior (58f033b)
- Refine transient todo reminders (7ab1a82)

## 0.4.7 (2026-04-19)

- Persist read_file overlap coverage state (351a081)
- Add scoped read_file overlap pruning (0874ef7)
- Add ranged read_file support and payload dedupe (1aed30b)
- Document TodoWrite reminder behavior (58f033b)
- Refine transient todo reminders (7ab1a82)

## 0.4.6 (2026-04-17)

- Implement topic-shift janitor assist and importance weighting (6da75fa)
- Record provider debug payload responses and errors (b698de4)

## 0.4.5 (2026-04-16)

- fix: 淇 release 鑴氭湰鐨?Windows 10 鍏煎鎬?(bc4cc22)
- feat: 澧炲姞妯″瀷涓婁笅鏂囩獥鍙ｆ槧灏勮〃 (da97f3d)
- refactor: 绉婚櫎 token 闃堝€煎苟鏀圭敤 janitor 瑙﹀彂姣斾緥 (623acf6)
- fix: 缁熶竴 Provider 寮傚父鍖呰骞跺鍔犻噸璇曞欢鏃?(32e6ccf)
- feat: 澧炲姞澶辫触閫氱煡骞朵慨澶?Hook SDK 瀵煎叆 (e4731f2)
- feat(debug): dump provider payloads behind hidden env (1ff3793)
- refactor(janitor): move auto janitor to turn boundary (0ac9ba6)
- docs: update janitor governance thresholds (fe5e7c5)
- fix(edit_file): accept stringified edits payload (2bde3af)
- provider 淇敼 (236a937)
- edit_file 宸ュ叿榛樿鍙彁渚涙壒閲忕紪杈?(4805512)
- Hide diagnostic commands from slash completion (aed8c71)

## 0.4.4 (2026-04-16)

- Add repository line ending rules (3f5ad51)
- Add async hooks context refs and SDK (d48a479)
- Override managed hooks from workspace config (288b519)
- Add hook toggles and /hooks browser (b6b25e6)
- feat: 增加 Hooks 系统与内置通知钩子 (b8efcae)

## 0.4.3 (2026-04-15)

- 为 janitor 增加低收益自动熔断 (a3e870f)
- 调整 janitor 手动触发与上下文阈值 (009d7ff)
- 统一 Update 显示并兼容 edit_file 路径格式 (12e3ab7)
- 优化上下文治理与 janitor 性能 (0a1ebd1)
- 优化文件编辑上下文治理与手动 janitor (5dd7afb)
- docs: 删除根目录下已迁移的项目概述文件 (6e741e6)
- docs: 将项目概述移入运维目录，整理运维文档序号 (c0ea985)
- Reorganize project documentation (af06705)
- Support single-file grep paths (7e36200)
- Make semantic janitor ratio-based only (66d665a)

## 0.4.2 (2026-04-14)

- Improve grep regex compatibility heuristics (b0a4096)
- 面板里单独显示治理提示，以及非稳定状态栏/toolbar 里也能看到 (eafda68)
- Add semantic context janitor and coverage (191b534)

## 0.4.1 (2026-04-13)

- Add checkpoint rollback support (a8c59b6)
- feat: track session token usage totals (da9873f)

## 0.4.0 (2026-04-10)

- 更新文档 (e0c15d8)
- feat: support multi-term symbol search (1f3fc4d)
- fix: preserve active task window during auto compact (2bf85f7)
- refactor: remove exploration memory and tool microcompact (b47530c)
- feat: improve investigation state and payload compaction (25105b3)
- feat: add repository exploration commands and memory (21d130a)
- Infer release version in release.sh (f156bac)

## 0.3.9 (2026-04-08)

- Browse MCP servers from interactive picker (0f415c0)
- Add shared provider management dialog (b4c9702)
- Document stdio MCP configuration (fb5503e)
- Add minimal stdio MCP server smoke coverage (423d84c)

## 0.3.8 (2026-04-08)

- Add -c continue-session flag and refresh release flow docs (86da771)

## 0.3.7 (2026-04-08)

- Maintenance release.

## 0.3.6 (2026-04-08)

- Infer next release version when version arg is omitted (ecc17c1)
- Auto-generate changelog entries from git history in release scripts (1307987)
- Unify provider setup form and improve Ctrl+C input behavior (96e6298)
- Handle stale provider configs without api keys (72497e4)
- Bootstrap provider setup on missing or stale config (68db41e)

## 0.3.5 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.3.4 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.1.0 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.3.3 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.3.2 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.2.0 (2026-04-08)

- (请手动补充 changelog 条目)


## 0.1.0

- Initial OpenAgent MVP implementation.
- Added modular runtime, provider adapters, persistent task/session stores, background jobs, teammate workflows, and MCP stdio client support.
