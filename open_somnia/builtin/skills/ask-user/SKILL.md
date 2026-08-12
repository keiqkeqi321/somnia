---
name: ask-user
description: Not sure which skill fits? This routes a situation to the right skill or flow across Somnia's engineering skills. Use when picking an approach, or on 'which skill', 'how should I tackle this', 'what's the right way'.
---

# Ask User

You don't remember every skill, so ask. A **flow** is a path through the skills.
Most paths run along one **main flow**; a few **on-ramps** merge onto it.

## Trivial path — clear goal, small change

If the user's goal is unambiguous **and** the change is small (a single focused
edit, a one-off fix, a tiny addition), **don't reach for the skill machinery**.
Lay out a short plan with `TodoWrite`, execute it directly, done. Everything
below is for work that is bigger, fuzzier, or genuinely benefits from a spec /
tests / review. When in doubt, prefer this path — the flows exist to add
discipline, not ceremony, to small clear tasks.

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

## The main flow: idea → ship

The route most work travels.

1. **`/+grill-with-docs`** — sharpen the idea by interview. Start here whenever
   you're in a working directory (it leaves a paper trail in `CONTEXT.md`/ADRs).
2. **Branch — does a question need a runnable answer?** (state, business logic, a
   UI you have to see) → detour through **`/+prototype`**, then bring the answer
   back.
3. **Branch — is this a multi-session build?**
   - **Yes** → **`/+to-spec`** (synthesize the conversation into a spec + decide
     the testing seams), then **`/+to-tasks`** (break it into tracer-bullet tasks
     with blocking edges, stamped ready-for-agent). Work the frontier:
     **`/+implement`** one task per fresh context window, `/clear` between.
   - **No** → **`/+implement`** right here in this context.

   Either way, **`implement`** builds each task by driving **`/+tdd`** at the
   pre-agreed seams, then closes out with **`/+code-review`** before committing
   and calling `task_close`.

## On-ramps

- **Something's broken / slow / throwing / failing** → **`/+diagnosing-bugs`**.
- **A huge, foggy effort — too big for one session and the path isn't visible** →
  **`/+wayfinder`** (chart a map of decision tasks; slower and denser, so save it
  for exactly this, never a well-scoped feature).

## Standalone

- **`/+prototype`** — a throwaway to answer one design question.
- **`/+resolving-merge-conflicts`** — mid-merge or mid-rebase, hunk by hunk.

## Vocabulary underneath (loaded by name, not by hand)

- **`/+codebase-design`** — the deep-module vocabulary (module / interface /
  seam / depth / adapter).
- **`/+domain-modeling`** — ubiquitous language, `CONTEXT.md`, ADRs.
- **`/+grilling`** — the one-question-at-a-time interview primitive.

Reach for these directly only when the *words*, not the process, are the problem;
otherwise let the skills above pull them in.

## Context hygiene

Keep the grilling → spec → tasks steps in **one unbroken context window** (don't
compact or clear until after `to-tasks`), so the spec and the task graph build on
the same thinking. Each `implement` then starts fresh from its task. At any phase
boundary the move is, in order: **Continue** (if the next phase needs this one as
primary source) → `/clear` (if disposable) → handoff/subagent → `/compact`
(default, but last resort).
