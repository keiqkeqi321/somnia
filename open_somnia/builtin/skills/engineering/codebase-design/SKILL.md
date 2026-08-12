---
name: codebase-design
description: Shared vocabulary for designing deep modules (module, interface, depth, seam, adapter, leverage, locality). Use when designing/improving a module interface, finding deepening opportunities, deciding where a seam goes, making code testable/AI-navigable, or when another skill needs the deep-module terms.
---

# Codebase Design

Design **deep modules**: a lot of behaviour behind a small interface, placed at
a clean seam, testable through that interface. Use this language wherever code
is being designed or restructured. The aim is leverage for callers, locality for
maintainers, and testability for everyone.

## Glossary

Use these terms exactly — don't substitute "component," "service," "API," or
"boundary." Consistent language is the whole point.

**Module** — anything with an interface and an implementation. Deliberately
scale-agnostic: a function, class, package, or tier-spanning slice.

**Interface** — everything a caller must know to use the module correctly: the
type signature, but also invariants, ordering constraints, error modes, required
configuration, and performance characteristics.

**Implementation** — what's inside a module. Distinct from **Adapter**: a thing
can be a small adapter with a large implementation (a Postgres repo) or a large
adapter with a small implementation (an in-memory fake).

**Depth** — leverage at the interface: the amount of behaviour a caller (or test)
can exercise per unit of interface they must learn. **Deep** = lots of behaviour
behind a small interface; **shallow** = interface nearly as complex as the
implementation.

**Seam** (Michael Feathers) — a place where you can alter behaviour without
editing in that place; the *location* at which a module's interface lives. Where
to put the seam is its own decision, distinct from what goes behind it.

**Adapter** — a concrete thing that satisfies an interface at a seam. Describes
*role* (what slot it fills), not substance.

**Leverage** — what callers get from depth: more capability per unit of
interface learned. One implementation pays back across N call sites and M tests.

**Locality** — what maintainers get from depth: change, bugs, knowledge, and
verification concentrate in one place rather than spreading across callers.

## Deep vs shallow

Deep module = small interface + lots of implementation. Shallow module = large
interface + little implementation (avoid — it's a pass-through). When designing
an interface ask: can I reduce the methods? simplify the parameters? hide more
complexity inside?

## Principles

- **Depth is a property of the interface, not the implementation.** A deep module
  can be internally composed of small, mockable parts — they just aren't part of
  the interface.
- **The deletion test.** Imagine deleting the module. If complexity vanishes, it
  was a pass-through. If complexity reappears across N callers, it was earning
  its keep.
- **The interface is the test surface.** Callers and tests cross the same seam.
  If you want to test *past* the interface, the module is probably the wrong
  shape.
- **One adapter means a hypothetical seam. Two adapters means a real one.** Don't
  introduce a seam unless something actually varies across it.

## Designing for testability

1. **Accept dependencies, don't create them** (pass the gateway in; don't
   construct it inside).
2. **Return results, don't produce side effects** (compute the discount; return
   it; don't mutate the cart).
3. **Small surface area** (fewer methods = fewer tests; fewer params = simpler
   setup).

## Going deeper

Two sub-disciplines, applied when the design warrants them:

- **Deepening a cluster given its dependencies** — dependency categories, seam
  discipline, replace-don't-layer testing.
- **Design-it-twice** — explore alternative interfaces by spinning up parallel
  subagents that design the interface several radically different ways, then
  compare on depth, locality, and seam placement.
