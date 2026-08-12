---
name: grill-with-docs
description: A relentless interview that also builds the project's domain docs as you go - runs grilling and updates CONTEXT.md/ADRs inline as decisions land. Use when sharpening a plan/idea in a working directory and you want the paper trail, or on 'grill this', 'interview me about this plan'.
---

# Grill With Docs

The stateful version of `grilling`: it runs the same relentless interview and
*also* maintains the project's domain model as decisions land, using the
`domain-modeling` skill — sharpening terminology and updating `CONTEXT.md` and
ADRs inline.

Use this whenever you are **working in a working directory**: it leaves a paper
trail, which makes it strictly better than bare `grilling` whenever a repo is
there to leave it in. (No working directory? Use `grilling` — it's stateless.)

Run a `grilling` session, driving `domain-modeling` as you go.
