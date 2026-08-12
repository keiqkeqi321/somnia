---
name: domain-modeling
description: Build and sharpen a project's domain model - challenge fuzzy/overloaded terms, stress-test with scenarios, update CONTEXT.md and ADRs inline. Use when pinning down terminology/ubiquitous language, recording a hard-to-reverse decision, or on 'what does X mean here', 'are these the same thing?'.
---

# Domain Modeling

Actively build and sharpen the project's domain model as you design — challenging
terms, inventing edge-case scenarios, and writing the glossary and decisions down
the moment they crystallise. (Merely *reading* `CONTEXT.md` for vocabulary is not
this skill — any skill can do that. This skill is for when you're *changing* the
model.)

## File structure

Most projects have a single context:

```
/
├── CONTEXT.md            ← the glossary, nothing else
├── docs/adr/             ← architectural decisions
└── src/
```

Create files lazily — only when you have something to write. First term
resolved → create `CONTEXT.md`. First hard decision → create `docs/adr/`.

## During the session

**Challenge against the glossary.** When the user uses a term that conflicts with
`CONTEXT.md`, call it out immediately: "Your glossary defines 'cancellation' as
X, but you seem to mean Y — which is it?"

**Sharpen fuzzy language.** When terms are vague or overloaded, propose a precise
canonical term: "You're saying 'account' — do you mean the Customer or the User?
Those are different things."

**Discuss concrete scenarios.** When domain relationships are discussed,
stress-test them with specific scenarios that probe edge cases and force
precision about boundaries.

**Cross-reference with code.** When the user states how something works, check
whether the code agrees; surface contradictions.

**Update CONTEXT.md inline.** When a term is resolved, update it right there —
don't batch. `CONTEXT.md` must be totally devoid of implementation details: it is
a glossary and nothing else, not a spec or scratchpad.

**Offer ADRs sparingly — only when all three are true:**
1. **Hard to reverse** — changing your mind later costs meaningfully.
2. **Surprising without context** — a future reader will wonder "why this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you
   picked one for specific reasons.

If any of the three is missing, skip the ADR.
