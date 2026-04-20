# Changelog

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
