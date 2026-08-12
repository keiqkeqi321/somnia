---
name: engineering
description: Index + routing map of Somnia's engineering skills. Load to browse the catalog (what each does + how they chain) and pick one - ask-user, to-spec, to-tasks, implement, tdd, code-review, diagnosing-bugs, wayfinder, prototype, resolving-merge-conflicts + the vocabulary layer.
---

# Engineering Skills — Index

This is the index of Somnia's engineering skills. They all live under this
bundle; load any with `load_skill <name>` (or `/+<name>`). For "which skill fits
my situation", load `ask-user`.

## Catalog

| skill | what it does | composes with |
|---|---|---|
| `ask-user` | route a situation to the right skill/flow; trivial small tasks → `TodoWrite` + execute | → any below |
| `grill-with-docs` | relentless interview to sharpen an idea; writes `CONTEXT.md`/ADRs | `grilling`, `domain-modeling` |
| `to-spec` | synthesize the conversation into a spec + decide the testing seams | → `to-tasks` |
| `to-tasks` | break a spec into a tracer-bullet task graph (`blocked_by`, `ready-for-agent`, `acceptance`) | → `implement` |
| `implement` | claim a task; drive `tdd` → `code-review` → `task_close` → commit | `tdd`, `code-review` |
| `tdd` | red→green loop at pre-agreed seams | `codebase-design` |
| `code-review` | two-axis review (Standards + Spec) via parallel subagents | — |
| `diagnosing-bugs` | red-loop discipline → fix → regression test | → `tdd` |
| `wayfinder` | decision-task map (`parent_id`) for huge, foggy efforts | → `to-spec` when clear |
| `prototype` | throwaway code answering one design question | — |
| `resolving-merge-conflicts` | merge/rebase conflicts hunk-by-hunk by intent; never `--abort` | — |
| `codebase-design` | deep-module vocabulary (module/interface/seam/depth) | reference |
| `domain-modeling` | ubiquitous language, `CONTEXT.md`, ADRs | reference |
| `grilling` | one-question-at-a-time interview primitive | reference |

## The map

```mermaid
flowchart TD
    classDef router fill:#d1fae5,stroke:#059669,color:#000
    classDef flow fill:#fef3c7,stroke:#d97706,color:#000
    classDef onramp fill:#dbeafe,stroke:#2563eb,color:#000
    classDef standalone fill:#e5e7eb,stroke:#6b7280,color:#000
    classDef vocab fill:#f3e8ff,stroke:#7c3aed,color:#000
    classDef fast fill:#bbf7d0,stroke:#16a34a,color:#000
    classDef gate fill:#fee2e2,stroke:#dc2626,color:#000

    Enter([user request / agent enters]):::router
    Enter --> Ask["ask-user - router"]:::router
    Ask --> Sit{situation?}:::gate

    Sit -->|"clear goal + small change"| Fast["TodoWrite plan -> execute directly"]:::fast

    Sit -->|"idea / build a feature"| Grill["grill-with-docs - sharpen the idea"]:::flow
    Grill --> Qproto{needs a runnable answer?}:::gate
    Qproto -->|yes| Proto["prototype - throwaway"]:::standalone
    Qproto -->|no| Qsess
    Proto --> Qsess{multi-session build?}:::gate
    Qsess -->|"yes, clear"| ToSpec["to-spec - synthesize spec + seams"]:::flow
    Qsess -->|"no, single session"| Impl["implement"]
    ToSpec --> ToTasks["to-tasks - tracer-bullet graph<br/>blocked_by + ready-for-agent + acceptance"]:::flow
    ToTasks --> Impl
    Impl --> TDD["tdd - red->green at the seam"]:::flow
    TDD --> CR["code-review - two-axis parallel"]:::flow

    Sit -->|"broken / slow / failing"| Diag["diagnosing-bugs - red loop -> fix"]:::onramp
    Diag -.->|fix lands| TDD
    Sit -->|"huge + path not visible"| Way["wayfinder - decision map (parent_id) + fog-of-war"]:::onramp
    Way -.->|way clear| ToSpec

    Sit -->|"merge / rebase conflict"| Merge["resolving-merge-conflicts - by intent, never --abort"]:::standalone

    subgraph Vocab["vocabulary / primitive layer - loaded by name"]
        V1["codebase-design"]:::vocab
        V2["domain-modeling"]:::vocab
        V3["grilling"]:::vocab
    end
    V1 -.-> TDD & CR
    V2 & V3 -.-> Grill

    Fast --> Done([done]):::router
    CR --> Done
```

## How to use

Load the skill you need with `load_skill <name>` (or `/+<name>`). If you're not
sure which, `ask-user` routes by situation. Keep the grilling → spec → tasks
steps in one unbroken context window; each `implement` then starts fresh from its
task. For a small, unambiguous change, skip the machinery — `TodoWrite` a short
plan and execute it directly.
