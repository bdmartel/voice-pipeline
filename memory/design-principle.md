---
name: Design Principle
description: Prove the payload first — avoid the compound friction that killed the first attempt
type: feedback
---

Build the dumbest working version first. Validate the core value before adding convenience.

**Why:** The first attempt (Mar 11) stalled because it went straight to building a full app — Tk GUI, pynput global hotkeys, threading. Each fix uncovered the next problem (Tk crash → threading fix → pynput permissions). The "20-30 min build" turned into yak-shaving and got abandoned because Superwhisper was "good enough." The second attempt (Mar 29) shipped a working script in minutes by skipping all of that.

**How to apply:** When adding features (global hotkey, auto-paste, etc.), only add one at a time, only after the current version has been used enough to know what's actually annoying. Don't build the rocket before proving the payload works.
