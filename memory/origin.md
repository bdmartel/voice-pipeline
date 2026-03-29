---
name: Origin Story
description: Why the voice pipeline exists — replacing Superwhisper with a custom local pipeline
type: project
---

Custom voice-to-prompt pipeline replacing the Superwhisper app ($8.49/mo).

**Why:** Superwhisper had persistent issues — trailing periods breaking slash commands, clipboard overwrites, no control over the rewrite layer. The key insight from Mar 11 research: the rewrite model (LLM) matters infinitely more than the ASR model. Superwhisper doesn't let you control the LLM layer.

**How to apply:** This tool's value is the Haiku rewrite step with a custom system prompt. ASR (Faster Whisper) just captures words — keep it simple. All investment goes into prompt quality and workflow integration.
