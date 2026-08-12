---
name: prototype
description: Build a throwaway prototype to answer one design question - a single HTML for state/logic, or toggleable UI variations. Use when 'how should this behave/look' is the blocker, or on 'prototype this', 'spike this', 'try a quick version'.
---

# Prototype

Build a **throwaway** prototype to answer one design question: does this state
model feel right, or what should this UI look like? Throwaway is a constraint on
how the code is written, not a promise to destroy it — the answer folds into the
real code, and the prototype itself is kept as a **primary source** on a
`prototype/<name>` branch off main, linked from the implementation task.

## Pick the question

One question. If there are two, that's two prototypes.

- **Logic / state question** ("does this reducer / state machine / data model
  behave right?") → a single self-contained HTML file you can open and click
  through. Keep it dependency-free, inline everything, and **make the state
  visible** (log every transition, render the current state, expose the inputs).
  The goal is to feel whether the model handles the awkward cases.
- **UI question** ("what should this look like?") → several **toggleable
  variations** in one file, so the choice is comparative, not yes/no on one take.
  Three rough options beat one polished one.

## Build it

Use the file and shell tools. Keep it cheap and rough — the point is to react to
something concrete, not to ship it. Resist building the real thing: no real
backend, no real persistence, no production polish, no tests. The moment you're
solving the actual problem rather than answering the question, you've stopped
prototyping.

## Use the answer

The **answer** (not the code) is what you keep: fold the decision into the real
work. Reference the `prototype/<name>` branch from the implementation task's
description or `commit_ref` so the next reader can see the primary source. If the
prototype encoded a decision more precisely than prose (a state machine, a
reducer, a schema, a type shape), inline the decision-rich snippet into the spec
or task and note it came from a prototype.
