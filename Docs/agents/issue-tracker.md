# Issue Tracker: Local Markdown

Issues and specs live as Markdown files under `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The spec is `.scratch/<feature-slug>/spec.md`.
- Implementation issues are separate files under
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`.
- Each issue records its triage role in a `Status:` line.
- Comments and conversation history are appended under `## Comments`.

## Dependencies

Each issue has a `Blocked by:` line containing the numbers of its blockers.
An issue is ready when every listed blocker has been completed.

When a skill says to publish to the issue tracker, create or update the
corresponding Markdown file under `.scratch/`.
