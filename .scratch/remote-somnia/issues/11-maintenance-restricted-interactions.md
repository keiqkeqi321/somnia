# 11 — Match maintenance controls and restricted interactions

**What to build:** Expose Session maintenance, model controls, and waiting interactions while enforcing the rule that permissions, sensitive settings, and Yolo require confirmation on the computer.

**Blocked by:** 07 — Match the Desktop Session lifecycle; 09 — Match active Turn control.

**Status:** ready-for-agent

- [ ] Compact and janitor operations expose Desktop-equivalent status and results.
- [ ] Provider, model, vision model, and reasoning controls follow the approved remote policy.
- [ ] Pending authorization and mode-switch interactions are visible in the correct Session.
- [ ] The Web interface clearly identifies actions awaiting confirmation on the computer.
- [ ] Remote attempts to persist permissions, change sensitive configuration, or enable Yolo are denied.
- [ ] Policy enforcement is tested at the Connector rather than trusted to browser presentation.
