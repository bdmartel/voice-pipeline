# Voice Pipeline

Custom voice-to-prompt tool for macOS. Speak naturally, get clean text or structured prompts pasted wherever your cursor is. Replaces tools like Superwhisper with a fully local transcription pipeline and optional AI rewriting via Claude.

## How It Works

```
[Mic] -> [Whisper (local)] -> raw transcription
                                    |
                        [Word corrections] -> corrected text
                                    |
                    [Intent selected?] -> [Claude Haiku rewrite] -> clean output
                                    |                                    |
                                    +-----> [Clipboard + Auto-paste] <---+
```

1. **Record** -- middle-click (or any mapped button) to start, click again to stop
2. **Transcribe** -- faster-whisper runs locally on CPU, no audio leaves your machine
3. **Correct** -- word corrections fix consistent Whisper mishearings (e.g., "Clawed" -> "Claude")
4. **Rewrite** -- the selected voice intent determines how the text is processed
5. **Paste** -- result is copied to clipboard and auto-pasted at your cursor

## Voice Intents

| Intent | What it does |
|---|---|
| **Clean Up** | Removes filler words, false starts, repetition. Preserves your phrasing. |
| **Code Prompt** | Converts speech into a precise Claude Code prompt. |
| **Context Code** | Like Code Prompt, but reads your active session state for context-aware prompts. |
| **Rewrite / Expand** | Rewrites and expands rough dictation into polished text. |
| **Raw** | No processing -- raw transcription passthrough. |

Intents are fully customizable in the web UI. Create your own with any system prompt + model.

## Features

- **Local transcription** -- faster-whisper on CPU, zero cloud dependency for STT
- **Multiple voice intents** -- different rewrite modes for different contexts
- **Word corrections** -- fix Whisper's consistent errors with auto-replacements
  - **Whole mode** (default) -- only matches when the entire input is the phrase
  - **Partial mode** -- matches anywhere in text (for proper nouns Whisper always gets wrong)
- **Transcription log** -- review, edit, reprocess, or combine past transcriptions
- **Editable transcriptions** -- click to edit raw text inline, then reprocess
- **Combine** -- select multiple transcriptions and merge them into one
- **Usage tracking** -- cost, tokens, and duration tracked per-intent, persisted across restarts
- **Session context** -- Context Code intent reads `SESSION_STATE.md` from your active project
- **Gmail detection** -- auto-switches to email formatting when Gmail is frontmost
- **Confused-response fallback** -- if Claude outputs meta-garbage instead of cleaning your text, auto-reverts to raw
- **Input remapping** -- map any mouse button, key combo, or scroll event to actions (block, keystroke, shell command, voice pipeline)
- **Web UI** -- dark theme dashboard at `localhost:9876` with live event feed, device inspector, and full configuration

## Requirements

- **macOS 12+** (uses CGEventTap, Quartz, Accessibility permissions)
- **Python 3.9+**
- **Anthropic API key** (set `ANTHROPIC_API_KEY` env var)

### Python packages

```bash
pip3 install numpy sounddevice faster-whisper anthropic
```

`Quartz` and `CoreFoundation` bindings come with macOS Python.

### Permissions

- **Accessibility** -- required for CGEventTap (input interception). Grant in System Settings > Privacy & Security > Accessibility.
- **Microphone** -- required for voice recording. macOS will prompt on first use.

## Setup

```bash
git clone https://github.com/bdmartel/voice-pipeline.git
cd voice-pipeline

# Install dependencies
pip3 install numpy sounddevice faster-whisper anthropic

# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run
python3 input-mapper/mapper.py
```

The web UI opens automatically at [http://localhost:9876](http://localhost:9876).

## Quick Start

1. **Start the daemon** -- `python3 input-mapper/mapper.py`
2. **Open the UI** -- browser opens automatically, or go to `localhost:9876`
3. **Map a button** -- go to Events tab, click something (e.g., middle-click), assign it to "Voice Pipeline" with an intent
4. **Talk** -- click your mapped button, speak, click again. Text appears at your cursor.

## Configuration

Everything is configured through the web UI:

- **Events tab** -- live feed of all input events. Click one to inspect and assign an action.
- **Mappings tab** -- manage all active mappings.
- **Voice Intents tab** -- create, edit, and delete voice intents. Each has a system prompt, model, output mode, and optional session context injection.
- **Transcriptions tab** -- review all past transcriptions. Edit raw text, reprocess through a different intent, combine multiple transcriptions, copy raw or processed output.
- **Corrections tab** -- manage word corrections. Whole mode for command replacements, partial mode for proper nouns.
- **Usage tab** -- cumulative cost, tokens, and duration breakdown by intent.

Config is saved to `input-mapper/config.json`. Usage stats persist in `input-mapper/usage.json`.

## Architecture

The daemon is a single Python process that:
- Runs a **CGEventTap** on the main thread to intercept all input events
- Serves a **web UI** via a threaded HTTP server on `localhost:9876`
- Pushes **live events** to the browser via Server-Sent Events (SSE)
- Records audio via **sounddevice**, transcribes with **faster-whisper**, rewrites with **Claude Haiku**
- Auto-pastes via synthetic `Cmd+V` keystroke (skipped in terminals where middle-click handles paste)

## File Structure

```
voice-pipeline/
  input-mapper/
    mapper.py            # the daemon (run this)
    ui.html              # single-file web UI
    config.json          # mappings, intents, corrections
    usage.json           # cumulative usage stats
    transcriptions.json  # recent transcription log
  voice-pipeline.py      # standalone MVP (predecessor, still works)
```

## License

Personal project. Use it however you want.
