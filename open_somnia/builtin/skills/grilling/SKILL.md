---
name: grilling
description: A relentless one-question-at-a-time interview to sharpen a plan/decision/idea - works a design tree by rounds, facts are the agent's job, decisions are yours. Use to stress-test thinking, or on 'grill me', 'stress-test this plan', 'challenge this design', 'poke holes in this'.
---

# Grilling

Interview the user relentlessly until you reach a shared understanding. Map the
work as a **design tree**: every decision branches into the decisions that hang
off it.

Work the tree in **rounds**. The **frontier** is every decision whose
prerequisites are already settled — the questions you can ask *now* without
guessing at answers you haven't heard. Ask the whole frontier in one round:
number each question and give your recommended answer. Then wait for the user's
answers before the next round.

Each question:

```
❓ **Q1** - **<question title>**: <body, may be multiple paragraphs / choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree — settled decisions push the
frontier outward and unblock questions that depended on them. Recompute the
frontier and ask the next round. A question whose answer depends on something
still open this round belongs to a *later* round, not this one.

**Finding facts is your job, never the user's.** When a frontier question needs
a fact from the environment (filesystem, tools, docs), find it — dispatch a
subagent to look it up rather than asking the user. Don't block on it: a running
lookup is an unsettled prerequisite, so only the questions downstream of it wait
— ask the rest of the frontier now. The *decisions* are the user's — put each to
them and wait.

The session is done when the frontier is empty: every branch visited, nothing
left silently assumed. Do not act on it until the user confirms you've reached a
shared understanding.
