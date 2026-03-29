# Session State
**Updated:** 2026-03-29
**Chat:** voice-pipeline-build

## Currently Working On
MVP complete and tested. User wants to set up a global hotkey trigger — considering BetterTouchTool for visual hotkey assignment UI.

## Done This Session
- Found old voice pipeline project (stalled Mar 11 on macOS permission issues)
- Diagnosed why it stalled — compound friction from Tk/pynput/permissions
- Built minimal script: sounddevice → faster-whisper → Claude Haiku rewrite → pbcopy
- User tested MVP and confirmed working
- Added cost tracking (token count + $ per call)
- Added app-aware context filtering (terminal → rewrite, other → raw passthrough)
- Added auto-stop on 2s silence, --enter and --raw flags, cmux notifications
- Alias `vp` added to .zshrc
- Moved project to ~/projects/voice-pipeline/

## Next Steps
- Install BetterTouchTool or grant Ghostty Accessibility permissions
- Set up global hotkey to trigger `vp` from anywhere
- Use daily and tune based on real friction, not speculation

## Key Decisions / Context
- "Prove the payload before building the rocket" — validated rewrite quality before adding convenience
- Silence threshold 500 RMS works for user's Blue Snowflake mic
- No pynput/Tk/GUI — deliberate choice to avoid the permission wall that killed v1
