# Changelog

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
