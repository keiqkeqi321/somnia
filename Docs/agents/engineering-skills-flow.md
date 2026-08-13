# Matt Pocock's Engineering Skills — Flow Map

A corrected map of how the skills in `skills-main/skills/engineering/` compose into
workflows. Source of truth for the composition is each skill's `SKILL.md` (especially
`ask-matt/SKILL.md`, which is the router). This doc exists to orient before porting or
adapting any of them.

## Legend

| Color | Meaning |
|-------|---------|
| 🟢 green | start / end |
| 🟡 yellow | user-invoked skill (`disable-model-invocation: true`) |
| 🔵 blue | model-invoked skill (rich triggers, the model reaches for it) |
| 🔴 red | decision gate |
| 🟣 purple | primitive / vocabulary layer — loaded by name, never manually triggered |
| ⚪ gray | standalone, off every flow |

## Flow

```mermaid
flowchart TD
    classDef user fill:#fef3c7,stroke:#d97706,color:#000
    classDef model fill:#dbeafe,stroke:#2563eb,color:#000
    classDef gate fill:#fee2e2,stroke:#dc2626,color:#000
    classDef vocab fill:#f3e8ff,stroke:#7c3aed,color:#000
    classDef start fill:#d1fae5,stroke:#059669,color:#000
    classDef standalone fill:#e5e7eb,stroke:#6b7280,color:#000

    Start([🤖 进入工程会话]):::start
    Start --> Q0{仓库已配置?<br/>docs/agents/* 存在?}:::gate
    Q0 -->|否| Setup["/setup-matt-pocock-skills<br/>一次性 bootstrap<br/>tracker · 标签 · 文档布局"]:::user
    Q0 -->|是| AskMatt
    Setup --> AskMatt

    AskMatt["❓ /ask-matt — 路由器<br/>我的处境对应哪条流程?"]:::user
    AskMatt --> Sit{用户处境?}

    %% ===== 主干: idea → ship =====
    Sit -->|"💡 有想法/要造功能"| Grill["/grill-with-docs  ← step 1<br/>访谈磨利想法<br/>即时写 CONTEXT.md / ADR"]:::user

    Grill --> Qproto{有问题需要<br/>可运行的答案?}:::gate
    Qproto -->|是| Proto["/prototype<br/>handoff 出 → 一次性原型<br/>→ handoff 回(留作 primary source)"]:::model
    Qproto -->|否| Qsess
    Proto --> Qsess{多会话构建?}:::gate

    Qsess -->|"是,但已清晰"| ToSpec["/to-spec<br/>综合对话成 spec<br/>📌 在此敲定 testing seams"]:::user
    Qsess -->|"否,单会话装得下"| Implement

    ToSpec --> ToTickets["/to-tickets<br/>拆 tracer-bullet 垂直切片<br/>声明 blocking edges · 🏷️ ready-for-agent"]:::user
    ToTickets --> G1{用户批准<br/>粒度与依赖图?}:::gate
    G1 -->|否,迭代| ToTickets
    G1 -->|是| Frontier["工作 frontier<br/>取无阻塞的票,一张一张做<br/>⏳ 每张之间 /new 开新窗口"]

    Frontier --> Implement["/implement<br/>单票: tdd → code-review → commit"]:::user
    Implement --> TDD["/tdd<br/>red → green<br/>📍 只在 to-spec 预先同意的 seam 上"]:::model
    TDD --> CR["/code-review<br/>双轴并行子agent: Standards + Spec<br/>🚫 不合并 / 不重排发现"]:::model
    CR --> Commit([✅ commit 当前分支]):::start

    %% ===== On-ramps: 汇入主干 =====
    Sit -->|"📮 外来 issue/PR 堆积"| Triage["/triage<br/>状态机 needs-triage →<br/>needs-info / ready-for-agent /<br/>ready-for-human / wontfix"]:::user
    Triage -.->|"产出 ready-for-agent 票"| Frontier

    Sit -->|"🐛 东西坏了/变慢"| DiagBug["/diagnosing-bugs<br/>先建红回路 → minimise → hypothesise<br/>→ instrument → fix → regression<br/>⚠️ 无红信号禁入假设阶段"]:::model
    DiagBug -.->|"没好 seam 锁住 bug = 架构问题"| Health
    DiagBug -.->|"修复落地(自带 regression test)"| Commit

    Sit -->|"🏥 代码库健康度/技术债"| Health["/improve-codebase-architecture<br/>git 热点扫描 → HTML 报告<br/>🚫 报告阶段不提接口 → 选一个深化候选"]:::user
    Health -.->|"挑出的候选 = 一个新想法"| Grill

    Sit -->|"🗺️ 巨大且看不见路"| Wayfinder["/wayfinder<br/>在 tracker 上画决策票地图<br/>fog of war · 一次一票一会话<br/>只产决策,不交付"]:::user
    Wayfinder -.->|"路理清 → handoff,不 build"| ToSpec

    %% ===== 原语层: 被按名加载 =====
    subgraph Vocab["📚 原语层 — 被其他技能按名调用,不手动触发"]
        direction LR
        V1["/codebase-design<br/>deep-module 词汇:<br/>module · interface · seam · depth · adapter"]
        V2["/domain-modeling<br/>ubiquitous language<br/>CONTEXT.md + ADR"]
        V3["/grilling<br/>一次一问的决策树访谈<br/>事实自查 · 决策问人"]
    end
    Vocab:::vocab
    V1 -.->|"tdd · code-review · improve-arch 按名加载"| TDD
    V1 -.-> Health
    V2 -.-> Grill
    V2 -.-> Health
    V3 -.->|"grill-with-docs · triage · wayfinder · improve-arch 内部跑"| Grill

    %% ===== 独立: 不在流程上 =====
    Sit -->|"🔀 已在 merge/rebase 冲突中"| Merge["/resolving-merge-conflicts<br/>按意图追溯双方 primary source<br/>🚫 永不 --abort"]:::standalone
    Sit -->|"🔍 要查外部资料"| Research["/research<br/>后台子agent · 只追 primary source<br/>写成带引用 markdown"]:::standalone
    Research -.->|"喂给思考,不替代思考"| Grill

    %% ===== phase boundary: 阶段之间, 5 选 1 有序树, 第一个 yes 赢 =====
    Commit -.-> PB{phase 之间<br/>如何处理上下文?<br/>第一个 yes 赢}:::gate
    PB -->|"1 还能继续?"| Continue["Continue<br/>先排除: 零损耗, primary 源不降级"]:::start
    PB -->|"2 后续无关?"| New["agent 自行 request_new_session<br/>(用户侧 /new)<br/>最便宜, 旧会话仍可 resume<br/>带 handoff 文本即跨会话交接"]:::start
    PB -->|"3 换 harness/dir/同事/分叉?"| Handoff["/handoff<br/>买的是可移植性, 仍 lossy"]:::standalone
    PB -->|"4 能 AFK?"| Sub["subagent<br/>紧 scoped, 报告回"]:::standalone
    PB -->|"5 其它"| Compact["/compact<br/>默认但最后才用<br/>primary → lossy summary"]:::standalone
```

## Notes

- **Phase boundary (context management)** is shown once, anchored at `commit`, but the
  decision recurs at **every** phase seam (grill → implement, implement → QA, …). It is
  an ordered tree — the first `yes` wins — not five parallel options. Mermaid can't
  render "ordered", so the edge numbers encode the priority.
- **`/handoff` vs `/compact` are both lossy** — both turn the primary source (the session
  as it happened) into a secondary summary. The real distinction is *portability*
  (handoff: new harness / dir / colleague / side-task) vs *same-line continuation*
  (compact). `Continue` is ruled out first because it is the only zero-cost move.
- **Omitted** standalone helpers that sit on no flow path: `/grill-me`, `/to-questionnaire`,
  `/wait-what`, `/wizard`, `/teach`, `/writing-for-agents`.
- `ask-matt`'s own "Vocabulary underneath" lists only `domain-modeling` + `codebase-design`;
  `/grilling` is classified there as a standalone *primitive*. It is grouped into the
  primitive layer here because, like the other two, it is loaded by name rather than
  manually triggered.
