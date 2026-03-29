# Voice Pipeline

Custom voice-to-prompt tool replacing Superwhisper. Speak naturally → local transcription → AI rewrite → clipboard.

## Current Status

**Phase:** MVP working — daily-drivable
**Alias:** `vp` (in ~/.zshrc)

### Confirmed Decisions
- No GUI, no pynput, no Tk — shell script mentality, proven before polished
- Auto-stop on 2s silence (default), `--enter` for manual stop
- App-aware context: terminal apps (Ghostty/Terminal/iTerm2) get Haiku rewrite, everything else gets raw transcription
- `--raw` flag forces passthrough (skips rewrite)
- Cost tracking per call (Haiku 4.5 pricing)
- cmux notification on completion

### Blocked / Waiting On
- [ ] Global hotkey trigger — needs either BetterTouchTool ($10) or Ghostty Accessibility permissions for pynput
- [ ] BetterTouchTool recommended for visual hotkey assignment UI

## Next Steps
- [ ] Install BetterTouchTool (or grant Ghostty Accessibility) for global hotkey
- [ ] Use MVP daily and note what's actually annoying before adding features
- [ ] Optional: auto-paste into cmux pane instead of clipboard

## Architecture

```
[Mic] → [sounddevice (record)] → wav file
            ↓
[Faster Whisper (local ASR)] → raw transcript
            ↓
[get_frontmost_app()] → terminal? → [Claude Haiku rewrite] → clean prompt
                      → other app? → raw transcript passthrough
            ↓
[pbcopy + cmux notify]
```

## Key References
- Research plan: `~/projects/ops/plans/voice-pipeline.md`
- FuturMinds video: "I Built a FREE Voice Tool for Claude Code" (https://www.youtube.com/watch?v=uBfn3Bj70x4)
- NotebookLM research notebook: 56b87157
- Research outputs: `~/projects/notebooklm-data-extractor/output/voice-tool-comparison.png`, `voice-tool-slideshow.pdf`

## Project Files
- `voice-pipeline.py` — the script (run via `vp` alias)

## Stack
- Python 3, sounddevice, numpy, faster-whisper, anthropic SDK
- Zero external installs required (all already on system)

## Tuning
- `SILENCE_THRESHOLD = 500` — RMS level below which = silence (tune for mic/environment)
- `SILENCE_DURATION = 2.0` — seconds of silence before auto-stop
- `REWRITE_APPS` set — add app names to get rewrite behavior
- `REWRITE_PROMPT` — edit to change how Haiku cleans up speech

## History
- 2026-03-11: Original attempt stalled — Tk threading crashes, pynput Accessibility permission blocker
- 2026-03-29: Rebuilt from scratch with "prove the payload first" approach. MVP confirmed working.
