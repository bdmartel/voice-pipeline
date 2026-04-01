# Session State
**Updated:** 2026-04-01 11:00
**Chat:** voice-pipeline-features

## Currently Working On
Session complete.

## Done This Session
- Transcription log: saves to transcriptions.json (cap 10), Transcriptions tab with SSE live updates
- Usage & Cost tab: stat cards + intent breakdown; persisted to usage.json across restarts
- Word corrections: fuzzy matching, trailing punctuation consumed, whole vs partial modes
- Corrections skip AI rewrite — corrected text is final output
- Confused-response detection: Haiku meta-responses ("I'm ready to help...") auto-revert to raw
- "Context Code" intent: reads SESSION_STATE.md from active cmux workspace (focus-aware)
- Editable raw transcriptions: click to edit inline, reprocess uses edited text
- Combine transcriptions: checkbox-select 2+, merge into one for reprocessing
- Screenshot drag fix: all mouse-ups pass through event tap
- Double-paste fix: Cmd+V skipped in terminal apps
- Auto-open browser on daemon startup
- Persistent usage stats in usage.json

## Next Steps
- Map a mouse button combo to "context-code" intent
- Daily-drive and add Whisper mishearing corrections as discovered
- Consider adding more confused-response signal phrases as they appear

## Key Decisions / Context
- Corrections: whole mode = entire input must match; partial = matches anywhere (for proper nouns)
- Fuzzy matching normalizes punctuation; trailing periods/commas consumed by regex
- All mouse-ups pass through event tap; Cmd+V auto-paste skipped in TERMINAL_APPS
- Session context uses cmux workspace name so switching workspaces switches context
- Corrections via UI are live immediately; Python code changes need daemon restart
- Usage stats accumulate to usage.json on every transcription + reprocess, survive restarts
