#!/usr/bin/env python3
"""Input Mapper — capture, inspect, and remap input device events.

Daemon that intercepts all input events via CGEventTap, serves a web UI
for inspection and mapping, and executes remapped actions.

Usage:
    python3 mapper.py              # Start on port 9876
    python3 mapper.py --port 8080  # Custom port
"""

import ctypes
import ctypes.util
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import numpy as np
import sounddevice as sd
import anthropic

import Quartz
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventMaskBit,
    CGEventGetIntegerValueField,
    CGEventGetFlags,
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CFMachPortCreateRunLoopSource,
    CFRunLoopGetCurrent,
    CFRunLoopAddSource,
    CFRunLoopStop,
    CFRunLoopRun,
    kCFRunLoopCommonModes,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventTapOptionDefault,
    kCGEventLeftMouseDown,
    kCGEventRightMouseDown,
    kCGEventOtherMouseDown,
    kCGEventLeftMouseUp,
    kCGEventRightMouseUp,
    kCGEventOtherMouseUp,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventScrollWheel,
    kCGEventFlagsChanged,
    kCGMouseEventButtonNumber,
    kCGKeyboardEventKeycode,
    kCGScrollWheelEventDeltaAxis1,
    kCGScrollWheelEventDeltaAxis2,
    kCGHIDEventTap,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TRANSCRIPTIONS_PATH = os.path.join(BASE_DIR, "transcriptions.json")
USAGE_PATH = os.path.join(BASE_DIR, "usage.json")
UI_PATH = os.path.join(BASE_DIR, "ui.html")
MOBILE_PATH = os.path.join(BASE_DIR, "mobile.html")

# ---------------------------------------------------------------------------
# Keycode → name lookup
# ---------------------------------------------------------------------------
KEYCODE_NAMES = {
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X",
    8: "C", 9: "V", 11: "B", 12: "Q", 13: "W", 14: "E", 15: "R",
    16: "Y", 17: "T", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6",
    23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0",
    30: "]", 31: "O", 32: "U", 33: "[", 34: "I", 35: "P",
    36: "Return", 37: "L", 38: "J", 39: "'", 40: "K", 41: ";",
    42: "\\", 43: ",", 44: "/", 45: "N", 46: "M", 47: ".",
    48: "Tab", 49: "Space", 50: "`", 51: "Delete",
    53: "Escape",
    55: "Command", 56: "Shift", 57: "CapsLock", 58: "Option", 59: "Control",
    60: "RightShift", 61: "RightOption", 62: "RightControl",
    63: "Fn",
    96: "F5", 97: "F6", 98: "F7", 99: "F3", 100: "F8",
    101: "F9", 103: "F11", 105: "F13", 107: "F14",
    109: "F10", 111: "F12", 113: "F15",
    115: "Home", 116: "PageUp", 117: "ForwardDelete",
    118: "F4", 119: "End", 120: "F2", 121: "PageDown", 122: "F1",
    123: "LeftArrow", 124: "RightArrow", 125: "DownArrow", 126: "UpArrow",
}

EVENT_TYPE_NAMES = {
    1: "LeftMouseDown", 2: "LeftMouseUp",
    3: "RightMouseDown", 4: "RightMouseUp",
    5: "MouseMoved",
    10: "KeyDown", 11: "KeyUp",
    12: "FlagsChanged",
    22: "ScrollWheel",
    25: "OtherMouseDown", 26: "OtherMouseUp",
}

MOUSE_BUTTON_NAMES = {0: "Left", 1: "Right", 2: "Middle", 3: "Button4", 4: "Button5"}

# ---------------------------------------------------------------------------
# Shared state (thread-safe)
# ---------------------------------------------------------------------------
event_buffer = []
event_buffer_lock = threading.Lock()
MAX_EVENTS = 200

sse_queues = []
sse_lock = threading.Lock()

mappings = {}  # event_key → {action_type, action_value, label}
mappings_lock = threading.Lock()

word_corrections = {}  # "wrong phrase" → "correct phrase" (case-insensitive match)

devices = []  # populated on startup
run_loop_ref = [None]
shutdown_flag = threading.Event()

# Toggle state: event_key → subprocess.Popen object (or None if not running)
toggle_processes = {}
toggle_lock = threading.Lock()

# Async event processing queue — keeps tap callback fast
event_process_queue = queue.Queue()

# Transcription log
transcriptions = []
transcriptions_lock = threading.Lock()
transcription_next_id = [1]
MAX_TRANSCRIPTIONS = 10

# Cumulative usage stats (persisted to usage.json)
usage_stats = {
    "total_transcriptions": 0,
    "total_cost": 0.0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_duration": 0.0,
    "by_intent": {},
}
usage_lock = threading.Lock()

# Tracked modifier state (updated by FlagsChanged events)
current_mods = {"shift": False, "ctrl": False, "opt": False, "cmd": False}
current_mods_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Voice Pipeline (built-in, model preloaded, intent-based)
# ---------------------------------------------------------------------------
VP_SAMPLE_RATE = 16000
VP_CHANNELS = 1

# Default intents — can be overridden/extended via config.json
DEFAULT_INTENTS = {
    "clean": {
        "name": "clean",
        "label": "Clean Up",
        "prompt": """You are a speech-to-text cleanup tool. The user dictated something out loud.
Remove unnecessary words — filler, false starts, repetition, verbal tics — while preserving
the user's exact meaning and phrasing. Do NOT rewrite, expand, or improve. Just clean.

Rules:
- Remove: um, uh, like, you know, so, basically, I mean, kind of, sort of, right, actually
- Remove false starts and repeated phrases
- Fix obvious transcription errors
- Keep the user's vocabulary, tone, and sentence structure
- Output ONLY the cleaned text, nothing else""",
        "model": "claude-haiku-4-5-20251001",
        "output": "paste",
    },
    "expand": {
        "name": "expand",
        "label": "Rewrite / Expand",
        "prompt": """You are a writing assistant. The user dictated a rough thought out loud.
Rewrite and expand it into a clear, complete, contextually sensible piece of text.

Rules:
- Capture the user's intent and develop it into a fuller thought
- Fix grammar, structure, and flow
- If the input is a fragment or keyword, expand it into a complete sentence or paragraph
- If the input is a question, make it precise and well-formed
- If the input is an instruction, make it clear and actionable
- Maintain the user's voice — don't make it sound robotic
- Output ONLY the rewritten text, nothing else — no quotes, no preamble
- NEVER ask for clarification or say you don't understand — just do your best with what you have
- If the input is truly unintelligible, output it unchanged""",
        "model": "claude-haiku-4-5-20251001",
        "output": "paste",
    },
    "raw": {
        "name": "raw",
        "label": "Raw (no processing)",
        "prompt": "",
        "model": "",
        "output": "paste",
    },
    "code": {
        "name": "code",
        "label": "Code Prompt",
        "prompt": """You are a voice-to-prompt translator for Claude Code (a CLI coding assistant).
The user spoke a rough idea out loud. Your job is to convert their speech into a precise,
well-structured prompt that Claude Code will immediately understand and act on.

Rules:
- Detect the user's intent: are they asking to build something, fix a bug, refactor, explain, research, or run a command?
- Translate into the language Claude Code expects:
  - Build requests → clear spec with requirements ("Create a Python script that...", "Add a function to X that...")
  - Bug fixes → describe the symptom and where to look ("The login flow fails when... check auth.py")
  - Refactoring → state what to change and why ("Rename X to Y across the codebase", "Extract the validation logic into its own function")
  - Questions → precise technical questions ("How does the event loop handle...", "What does X do in Y?")
  - Commands → direct instructions ("Run the tests", "Commit with message...", "Push to main")
- Strip all filler, false starts, and verbal tics
- Preserve technical terms, file names, function names, and specifics exactly as spoken
- If the user mentions context (files, functions, errors), include it — Claude Code needs it
- Use imperative voice — tell Claude what to do, not what you'd like
- Output ONLY the prompt, nothing else — no quotes, no preamble, no explanation
- Match complexity to input — a simple request stays one line, a complex one gets structure
- NEVER ask for clarification — infer intent from context and produce the best prompt you can""",
        "model": "claude-haiku-4-5-20251001",
        "output": "paste",
    },
    "context-code": {
        "name": "context-code",
        "label": "Context Code",
        "prompt": """You are a voice-to-prompt translator for Claude Code (a CLI coding assistant).
The user spoke a rough idea out loud while working in an active coding session.
Your job is to convert their speech into a precise prompt, informed by what they're currently working on.

Rules:
- Use the session context to understand what the user is referring to — they may use shorthand,
  refer to "that file" or "the bug" or "what we just did" without specifics
- Resolve vague references using the session context (e.g., "fix it" → fix the specific thing in the context)
- Detect the user's intent: build, fix, refactor, explain, research, or run a command
- Strip all filler, false starts, and verbal tics
- Preserve technical terms, file names, function names exactly as spoken
- Use imperative voice — tell Claude what to do
- Output ONLY the prompt, nothing else — no quotes, no preamble
- Match complexity to input — a simple request stays one line
- NEVER ask for clarification — infer from context and produce the best prompt you can""",
        "model": "claude-haiku-4-5-20251001",
        "output": "paste",
        "context": True,
    },
}

# Gmail detection — when frontmost app is a browser with Gmail, override the prompt
GMAIL_PROMPT_OVERRIDE = """You are an email writing assistant. The user dictated an email out loud.
Format it as a complete, professional email body ready to send.

Rules:
- Fix grammar, punctuation, and sentence structure
- Format with proper paragraphs and line breaks
- If the user mentioned a greeting (hi, hey, dear), include it. Otherwise add an appropriate one.
- If the user mentioned a sign-off, include it. Otherwise add a brief professional one.
- Maintain the user's tone — casual stays casual, formal stays formal
- Output ONLY the email body text, nothing else — no subject line, no metadata"""

BROWSER_APPS = {"Google Chrome", "Safari", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}

# Terminal apps where middle-click pastes — skip Cmd+V auto-paste to avoid double paste
TERMINAL_APPS = {"Ghostty", "Terminal", "iTerm2", "Alacritty", "kitty", "WezTerm"}

PROJECTS_DIR = os.path.expanduser("~/projects")


def get_session_context():
    """Read SESSION_STATE.md from the active cmux workspace's project directory.

    Uses cmux to detect which workspace has focus — so it reads the context
    of the session you just clicked into, not the most recently modified one.
    """
    # Get active cmux workspace name → maps to ~/projects/<name>
    try:
        result = subprocess.run(
            ["cmux", "list-workspaces"],
            capture_output=True, text=True, timeout=2)
        for line in result.stdout.strip().split("\n"):
            if "[selected]" in line:
                # Parse: "* workspace:2  voice-pipeline  [selected]"
                parts = line.strip().lstrip("* ").split()
                if len(parts) >= 2:
                    workspace_name = parts[1]
                    project_dir = os.path.join(PROJECTS_DIR, workspace_name)
                    session_file = os.path.join(project_dir, "SESSION_STATE.md")
                    if os.path.exists(session_file):
                        with open(session_file) as f:
                            content = f.read().strip()
                        if content:
                            print(f"  VP: Session context from {session_file}")
                            return content
                    print(f"  VP: No SESSION_STATE.md in {project_dir}")
                break
    except Exception as e:
        print(f"  VP: cmux workspace detection failed: {e}")

    return None


voice_intents = {}  # loaded from config, falls back to defaults
whisper_model = None  # loaded once at startup

# Recording state
vp_recording = False
vp_active_intent = None  # which intent is currently recording
vp_frames = []
vp_stream = None
vp_lock = threading.Lock()
vp_last_toggle = 0
VP_DEBOUNCE = 0.8


def vp_load_model():
    """Preload whisper model at startup."""
    global whisper_model
    from faster_whisper import WhisperModel
    print("  Loading faster-whisper model...")
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    print("  Model loaded — voice toggle will start instantly")


def vp_start_recording(intent_name):
    """Start capturing audio with a specific intent. Non-blocking."""
    global vp_recording, vp_frames, vp_stream, vp_active_intent
    with vp_lock:
        if vp_recording:
            return
        vp_frames = []
        vp_recording = True
        vp_active_intent = intent_name

        def callback(indata, frame_count, time_info, status):
            if vp_recording:
                vp_frames.append(indata.copy())

        vp_stream = sd.InputStream(samplerate=VP_SAMPLE_RATE, channels=VP_CHANNELS,
                                   dtype='int16', callback=callback)
        vp_stream.start()
        intent = voice_intents.get(intent_name, {})
        label = intent.get("label", intent_name)
        print(f"  VP [{label}]: Recording started")


def vp_stop_and_process():
    """Stop recording, transcribe, apply intent, output. Runs in background thread."""
    global vp_recording, vp_stream, vp_active_intent
    with vp_lock:
        if not vp_recording:
            return
        vp_recording = False
        intent_name = vp_active_intent
        vp_active_intent = None
        if vp_stream:
            vp_stream.stop()
            vp_stream.close()
            vp_stream = None
        frames = list(vp_frames)

    intent = voice_intents.get(intent_name, DEFAULT_INTENTS.get("raw", {}))
    label = intent.get("label", intent_name)

    if not frames:
        print(f"  VP [{label}]: No audio captured")
        return

    audio = np.concatenate(frames)
    duration = len(audio) / VP_SAMPLE_RATE
    print(f"  VP [{label}]: {duration:.1f}s captured")

    # Save to temp wav
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, 'wb') as wf:
        wf.setnchannels(VP_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(VP_SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    # Transcribe
    print(f"  VP [{label}]: Transcribing...")
    segments, _ = whisper_model.transcribe(wav_path)
    raw = " ".join(seg.text.strip() for seg in segments)
    os.unlink(wav_path)

    # Strip common Whisper noise artifacts
    cleaned = raw.strip().strip(".")
    noise_phrases = {"you", "thanks", "thank you", "bye", "the", "a", "i", "so", "and"}
    if not cleaned or len(cleaned) < 3 or cleaned.lower() in noise_phrases:
        print(f"  VP [{label}]: No meaningful speech detected (raw: '{raw}')")
        return
    print(f"  VP [{label}]: Raw: {raw}")

    # Apply word corrections
    raw_before = raw
    raw = apply_corrections(raw)
    corrected = raw != raw_before
    if corrected:
        print(f"  VP [{label}]: Corrected: {raw}")

    # Detect frontmost app — check for Gmail context
    app = ""
    window_title = ""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2)
        app = result.stdout.strip()
    except Exception:
        pass

    # Get window title to detect Gmail
    in_gmail = False
    if app in BROWSER_APPS:
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'tell application "{app}" to get name of front window'],
                capture_output=True, text=True, timeout=2)
            window_title = result.stdout.strip()
            in_gmail = "gmail" in window_title.lower()
        except Exception:
            pass

    inp_tokens = 0
    out_tokens = 0
    call_cost = 0.0

    # If corrections matched, use corrected text directly — skip AI rewrite
    if corrected:
        print(f"  VP [{label}]: Skipping rewrite (correction is final)")
        output = raw
    else:
        # Apply intent — rewrite or passthrough
        prompt = intent.get("prompt", "").strip()
        model = intent.get("model", "").strip()

        # Gmail override
        if in_gmail and intent_name != "raw":
            prompt = GMAIL_PROMPT_OVERRIDE
            model = model or "claude-haiku-4-5-20251001"
            label = f"{label} → Gmail"
            print(f"  VP [{label}]: Gmail detected — using email format")

        # Context injection — read active SESSION_STATE.md
        if intent.get("context") and prompt:
            ctx = get_session_context()
            if ctx:
                prompt += f"\n\n--- ACTIVE SESSION CONTEXT ---\nThe user is currently working in a Claude Code session. Use this context to make the prompt more specific and relevant:\n\n{ctx}\n--- END CONTEXT ---"
                print(f"  VP [{label}]: Injected session context")

        if prompt and model:
            print(f"  VP [{label}]: Rewriting...")
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
                system=prompt,
                messages=[{"role": "user", "content": raw}]
            )
            inp_tokens = msg.usage.input_tokens
            out_tokens = msg.usage.output_tokens
            call_cost = (inp_tokens * 0.80 + out_tokens * 4.00) / 1_000_000
            print(f"  VP [{label}]: {inp_tokens}+{out_tokens} tokens = ${call_cost:.4f}")
            output = msg.content[0].text

            # Detect confused meta-responses and fall back to raw
            confused_signals = [
                "i'm ready to help", "i don't see", "please provide",
                "could you provide", "i'd be happy to", "go ahead and",
                "i can help", "speech-to-text", "i need the",
                "paste the text", "share the text", "waiting for",
            ]
            output_lower = output.lower()
            if any(sig in output_lower for sig in confused_signals):
                print(f"  VP [{label}]: Haiku confused — reverting to raw")
                output = raw
        else:
            print(f"  VP [{label}]: Passthrough (no rewrite)")
            output = raw

        # Apply corrections to rewritten output too
        output = apply_corrections(output)

    # Output based on intent setting
    output_mode = intent.get("output", "paste")

    # Log transcription
    add_transcription(raw, output, intent_name, app, duration,
                      inp_tokens, out_tokens, call_cost)

    subprocess.run(["pbcopy"], input=output.encode(), check=True)
    print(f"  VP [{label}]: Copied to clipboard: {output[:80]}")

    if output_mode == "paste" and app not in TERMINAL_APPS:
        # Auto-paste via Cmd+V for non-terminal apps.
        # In terminals, middle-click pass-through already pastes from clipboard,
        # so sending Cmd+V would double-paste.
        time.sleep(0.1)
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        paste_down = CGEventCreateKeyboardEvent(src, 9, True)
        paste_up = CGEventCreateKeyboardEvent(src, 9, False)
        CGEventSetFlags(paste_down, 1 << 20)
        CGEventSetFlags(paste_up, 1 << 20)
        CGEventPost(kCGHIDEventTap, paste_down)
        CGEventPost(kCGHIDEventTap, paste_up)
        print(f"  VP [{label}]: Auto-pasted")

    try:
        subprocess.run(["cmux", "notify", "--title", f"vp:{intent_name}",
                         "--body", output[:80]],
                       capture_output=True, timeout=3)
    except Exception:
        pass

    broadcast_event({"key": "voice_pipeline", "type": "VoicePipeline",
                     "description": f"VP [{label}]: {output[:50]}",
                     "timestamp": time.time(),
                     "time": time.strftime("%H:%M:%S"),
                     "mapped": True})

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
def load_config():
    global mappings, voice_intents, word_corrections
    # Start with default intents
    voice_intents = dict(DEFAULT_INTENTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
            with mappings_lock:
                mappings = data.get("mappings", {})
            # Merge saved intents over defaults
            saved_intents = data.get("intents", {})
            voice_intents.update(saved_intents)
            word_corrections = data.get("word_corrections", {})
        print(f"  Loaded {len(mappings)} mapping(s), {len(voice_intents)} intent(s), {len(word_corrections)} correction(s)")
    else:
        print("  No config file — using defaults")
        print(f"  Default intents: {', '.join(voice_intents.keys())}")


def save_config():
    with mappings_lock:
        data = {
            "mappings": dict(mappings),
            "intents": dict(voice_intents),
            "word_corrections": dict(word_corrections),
        }
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_transcriptions():
    global transcriptions
    if os.path.exists(TRANSCRIPTIONS_PATH):
        with open(TRANSCRIPTIONS_PATH) as f:
            transcriptions = json.load(f)
        if transcriptions:
            transcription_next_id[0] = max(t["id"] for t in transcriptions) + 1
        print(f"  Loaded {len(transcriptions)} transcription(s)")


def save_transcriptions():
    with transcriptions_lock:
        data = list(transcriptions)
    with open(TRANSCRIPTIONS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_usage():
    global usage_stats
    if os.path.exists(USAGE_PATH):
        with open(USAGE_PATH) as f:
            usage_stats = json.load(f)
        print(f"  Loaded usage: {usage_stats['total_transcriptions']} calls, ${usage_stats['total_cost']:.4f} total")


def save_usage():
    with usage_lock:
        data = dict(usage_stats)
    with open(USAGE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def accumulate_usage(intent_name, input_tokens, output_tokens, cost, duration):
    """Add a transcription's stats to the cumulative usage totals."""
    with usage_lock:
        usage_stats["total_transcriptions"] += 1
        usage_stats["total_cost"] = round(usage_stats["total_cost"] + cost, 6)
        usage_stats["total_input_tokens"] += input_tokens
        usage_stats["total_output_tokens"] += output_tokens
        usage_stats["total_duration"] = round(usage_stats["total_duration"] + duration, 1)
        bi = usage_stats.setdefault("by_intent", {})
        if intent_name not in bi:
            bi[intent_name] = {"count": 0, "cost": 0, "input_tokens": 0,
                               "output_tokens": 0, "duration": 0}
        bi[intent_name]["count"] += 1
        bi[intent_name]["cost"] = round(bi[intent_name]["cost"] + cost, 6)
        bi[intent_name]["input_tokens"] += input_tokens
        bi[intent_name]["output_tokens"] += output_tokens
        bi[intent_name]["duration"] = round(bi[intent_name]["duration"] + duration, 1)
    save_usage()


def add_transcription(raw, output, intent_name, app, duration,
                      input_tokens=0, output_tokens=0, cost=0.0):
    """Append a transcription entry. Called after each successful VP transcription."""
    entry = {
        "id": transcription_next_id[0],
        "timestamp": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw": raw,
        "output": output,
        "intent": intent_name,
        "app": app,
        "duration": round(duration, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": round(cost, 6),
    }
    transcription_next_id[0] += 1
    with transcriptions_lock:
        transcriptions.append(entry)
        if len(transcriptions) > MAX_TRANSCRIPTIONS:
            transcriptions[:] = transcriptions[-MAX_TRANSCRIPTIONS:]
    save_transcriptions()
    # Accumulate to persistent usage stats
    accumulate_usage(intent_name, input_tokens, output_tokens, cost, duration)
    # Broadcast to UI
    broadcast_event({"key": "transcription_added", "type": "TranscriptionAdded",
                     "description": f"New transcription: {raw[:50]}",
                     "timestamp": entry["timestamp"],
                     "time": time.strftime("%H:%M:%S"),
                     "mapped": False,
                     "transcription": entry})
    return entry


import re as _re

def _normalize(s):
    """Strip punctuation and collapse whitespace for fuzzy matching."""
    s = _re.sub(r'[.,!?;:\-\'"]+', ' ', s)
    return _re.sub(r'\s+', ' ', s).strip().lower()

def apply_corrections(text):
    """Apply word corrections to text.

    Each correction has a mode:
      - 'whole' (default): only matches when the entire input is the phrase
      - 'partial': matches anywhere within the text (for proper nouns etc.)
    """
    if not word_corrections:
        return text

    norm_text = _normalize(text)

    for wrong, entry in word_corrections.items():
        # Support both old format ("key": "value") and new ("key": {"to": ..., "mode": ...})
        if isinstance(entry, str):
            correct, mode = entry, "whole"
        else:
            correct, mode = entry.get("to", ""), entry.get("mode", "whole")
        if not correct:
            continue

        words = _normalize(wrong).split()
        if not words:
            continue

        if mode == "whole":
            # Only match if the entire input (normalized) matches
            norm_wrong = " ".join(words)
            if norm_text == norm_wrong:
                return correct
        else:
            # Partial: match anywhere in text with flexible punctuation
            pattern = r'[.,!?;:\-\'"\s]*'.join(_re.escape(w) for w in words)
            pattern += r'[.,!?;:\'"]*'
            text = _re.sub(pattern, correct, text, flags=_re.IGNORECASE)

    return text


# ---------------------------------------------------------------------------
# Device enumeration (IOKit via ctypes)
# ---------------------------------------------------------------------------
def enumerate_devices():
    global devices
    try:
        iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

        cf.CFAllocatorGetDefault.restype = ctypes.c_void_p
        iokit.IOHIDManagerCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        iokit.IOHIDManagerCreate.restype = ctypes.c_void_p
        iokit.IOHIDManagerSetDeviceMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        iokit.IOHIDManagerOpen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        iokit.IOHIDManagerOpen.restype = ctypes.c_int32
        iokit.IOHIDManagerCopyDevices.argtypes = [ctypes.c_void_p]
        iokit.IOHIDManagerCopyDevices.restype = ctypes.c_void_p

        allocator = cf.CFAllocatorGetDefault()
        manager = iokit.IOHIDManagerCreate(allocator, 0)
        iokit.IOHIDManagerSetDeviceMatching(manager, None)
        iokit.IOHIDManagerOpen(manager, 0)

        device_set = iokit.IOHIDManagerCopyDevices(manager)
        if not device_set:
            devices = []
            return

        cf.CFSetGetCount.argtypes = [ctypes.c_void_p]
        cf.CFSetGetCount.restype = ctypes.c_long
        count = cf.CFSetGetCount(device_set)

        # Get all device refs
        device_refs = (ctypes.c_void_p * count)()
        cf.CFSetGetValues.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFSetGetValues(device_set, device_refs)

        # Helper to get string property
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool

        iokit.IOHIDDeviceGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        iokit.IOHIDDeviceGetProperty.restype = ctypes.c_void_p

        # Create CFString keys
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
        ]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p

        def cf_str(s):
            return cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

        def get_str_prop(dev, key):
            k = cf_str(key)
            val = iokit.IOHIDDeviceGetProperty(dev, k)
            if not val:
                return ""
            buf = ctypes.create_string_buffer(256)
            if cf.CFStringGetCString(val, buf, 256, 0x08000100):
                return buf.value.decode("utf-8", errors="replace")
            return ""

        def get_int_prop(dev, key):
            k = cf_str(key)
            val = iokit.IOHIDDeviceGetProperty(dev, k)
            if not val:
                return 0
            cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            num = ctypes.c_long(0)
            cf.CFNumberGetValue(val, 4, ctypes.byref(num))  # 4 = kCFNumberSInt64Type
            return num.value

        USAGE_PAGE_NAMES = {1: "Generic Desktop", 2: "Simulation", 6: "Generic Device",
                            7: "Keyboard", 9: "Button", 12: "Consumer", 0xFF: "Vendor"}
        USAGE_NAMES = {
            (1, 1): "Pointer", (1, 2): "Mouse", (1, 4): "Joystick",
            (1, 5): "Gamepad", (1, 6): "Keyboard", (1, 7): "Keypad",
            (1, 8): "Multi-axis", (12, 1): "Consumer Control",
        }

        result = []
        for i in range(count):
            dev = device_refs[i]
            product = get_str_prop(dev, "Product")
            manufacturer = get_str_prop(dev, "Manufacturer")
            transport = get_str_prop(dev, "Transport")
            usage_page = get_int_prop(dev, "PrimaryUsagePage")
            usage = get_int_prop(dev, "PrimaryUsage")
            vendor_id = get_int_prop(dev, "VendorID")
            product_id = get_int_prop(dev, "ProductID")

            # Filter to interesting devices (keyboards, mice, gamepads, consumer)
            if usage_page not in (1, 7, 12):
                continue

            usage_label = USAGE_NAMES.get((usage_page, usage),
                         USAGE_PAGE_NAMES.get(usage_page, f"Page {usage_page}"))

            result.append({
                "product": product or "Unknown Device",
                "manufacturer": manufacturer or "",
                "transport": transport or "Unknown",
                "type": usage_label,
                "vendor_id": vendor_id,
                "product_id": product_id,
            })

        devices = sorted(result, key=lambda d: d["product"])
        print(f"  Found {len(devices)} input device(s)")
        for d in devices:
            transport_icon = "BT" if "bluetooth" in d["transport"].lower() else "USB"
            print(f"    [{transport_icon}] {d['product']} — {d['type']}")

    except Exception as e:
        print(f"  Device enumeration error: {e}")
        devices = []


# ---------------------------------------------------------------------------
# Event key generation (used for mapping lookup)
# ---------------------------------------------------------------------------
def _get_mod_prefix_from_flags(event):
    """Extract modifier prefix from event flags (for keyboard events)."""
    flags = CGEventGetFlags(event)
    mods = []
    if flags & (1 << 17): mods.append("shift")
    if flags & (1 << 18): mods.append("ctrl")
    if flags & (1 << 19): mods.append("opt")
    if flags & (1 << 20): mods.append("cmd")
    return "+".join(mods) + "+" if mods else ""


def _get_mod_prefix_tracked():
    """Get modifier prefix from independently tracked state (for mouse events)."""
    with current_mods_lock:
        mods = [k for k, v in current_mods.items() if v]
    # Sort consistently: shift, ctrl, opt, cmd
    order = ["shift", "ctrl", "opt", "cmd"]
    mods = [m for m in order if m in mods]
    return "+".join(mods) + "+" if mods else ""


def _update_mod_state(event):
    """Update tracked modifier state from a FlagsChanged event."""
    flags = CGEventGetFlags(event)
    with current_mods_lock:
        current_mods["shift"] = bool(flags & (1 << 17))
        current_mods["ctrl"] = bool(flags & (1 << 18))
        current_mods["opt"] = bool(flags & (1 << 19))
        current_mods["cmd"] = bool(flags & (1 << 20))


def make_event_key(event_type, event):
    """Create a unique string key for an event type, used to match mappings."""
    if event_type in (kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown):
        btn = CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber)
        mod_str = _get_mod_prefix_tracked()
        return f"mouse:{mod_str}button:{btn}:down"
    elif event_type in (kCGEventLeftMouseUp, kCGEventRightMouseUp, kCGEventOtherMouseUp):
        btn = CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber)
        mod_str = _get_mod_prefix_tracked()
        return f"mouse:{mod_str}button:{btn}:up"
    elif event_type == kCGEventKeyDown:
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        flags = CGEventGetFlags(event)
        mods = []
        if flags & (1 << 17): mods.append("shift")
        if flags & (1 << 18): mods.append("ctrl")
        if flags & (1 << 19): mods.append("opt")
        if flags & (1 << 20): mods.append("cmd")
        mod_str = "+".join(mods) + "+" if mods else ""
        return f"key:{mod_str}{keycode}:down"
    elif event_type == kCGEventKeyUp:
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        return f"key:{keycode}:up"
    elif event_type == kCGEventScrollWheel:
        dy = CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1)
        direction = "up" if dy > 0 else "down" if dy < 0 else "none"
        return f"scroll:{direction}"
    return f"unknown:{event_type}"


def describe_event(event_type, event):
    """Human-readable description of an event."""
    etype = EVENT_TYPE_NAMES.get(event_type, f"Event({event_type})")

    if event_type in (kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown,
                      kCGEventLeftMouseUp, kCGEventRightMouseUp, kCGEventOtherMouseUp):
        btn = CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber)
        btn_name = MOUSE_BUTTON_NAMES.get(btn, f"Button{btn}")
        mod_str = _get_mod_prefix_tracked()
        if mod_str:
            mod_str = mod_str.replace("shift", "Shift").replace("ctrl", "Ctrl").replace("opt", "Opt").replace("cmd", "Cmd")
        return f"{mod_str}{btn_name} Mouse {'Down' if 'Down' in etype else 'Up'}"

    elif event_type in (kCGEventKeyDown, kCGEventKeyUp):
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        key_name = KEYCODE_NAMES.get(keycode, f"Key({keycode})")
        flags = CGEventGetFlags(event)
        mods = []
        if flags & (1 << 20): mods.append("Cmd")
        if flags & (1 << 19): mods.append("Opt")
        if flags & (1 << 18): mods.append("Ctrl")
        if flags & (1 << 17): mods.append("Shift")
        mod_str = "+".join(mods) + "+" if mods else ""
        return f"{mod_str}{key_name} {'Down' if event_type == kCGEventKeyDown else 'Up'}"

    elif event_type == kCGEventScrollWheel:
        dy = CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1)
        dx = CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2)
        parts = []
        if dy: parts.append(f"{'Up' if dy > 0 else 'Down'}")
        if dx: parts.append(f"{'Left' if dx > 0 else 'Right'}")
        return f"Scroll {' '.join(parts)}" if parts else "Scroll"

    return etype


# ---------------------------------------------------------------------------
# Action execution (runs in background thread)
# ---------------------------------------------------------------------------
def execute_action(action):
    """Execute a mapped action. Runs in a background thread."""
    action_type = action.get("action_type", "")
    value = action.get("action_value", "")

    if action_type == "block":
        return  # event was already swallowed

    elif action_type == "keystroke":
        # value is keycode as int, with optional modifier flags
        try:
            parts = value.split("+")
            keycode = int(parts[-1])
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
            evt_down = CGEventCreateKeyboardEvent(src, keycode, True)
            evt_up = CGEventCreateKeyboardEvent(src, keycode, False)
            # Apply modifiers
            flags = 0
            for mod in parts[:-1]:
                mod = mod.lower()
                if mod == "cmd": flags |= (1 << 20)
                elif mod == "opt": flags |= (1 << 19)
                elif mod == "ctrl": flags |= (1 << 18)
                elif mod == "shift": flags |= (1 << 17)
            if flags:
                CGEventSetFlags(evt_down, flags)
                CGEventSetFlags(evt_up, flags)
            CGEventPost(kCGHIDEventTap, evt_down)
            CGEventPost(kCGHIDEventTap, evt_up)
        except Exception as e:
            print(f"  Keystroke action error: {e}")

    elif action_type == "shell":
        try:
            subprocess.Popen(["zsh", "-ic", value], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  Shell action error: {e}")

    elif action_type == "voice_toggle":
        global vp_last_toggle
        now = time.time()
        if now - vp_last_toggle < VP_DEBOUNCE:
            return  # ignore rapid clicks
        vp_last_toggle = now

        intent_name = value if value else "code"  # action_value = intent name
        if not vp_recording:
            vp_start_recording(intent_name)
        else:
            # Process in background so tap callback returns fast
            threading.Thread(target=vp_stop_and_process, daemon=True).start()
        return

    elif action_type == "shell_toggle":
        event_key = action.get("_event_key", "")
        with toggle_lock:
            proc = toggle_processes.get(event_key)
            if proc and proc.poll() is None:
                # Process is running — send newline to stdin (graceful stop),
                # then fall back to SIGTERM if it doesn't exit
                print(f"  Toggle OFF: stopping PID {proc.pid}")
                try:
                    proc.stdin.write(b"\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                    print(f"  Process exited gracefully")
                except subprocess.TimeoutExpired:
                    print(f"  Graceful stop timed out, killing PID {proc.pid}")
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                toggle_processes[event_key] = None
                broadcast_event({"key": event_key, "toggle_state": "off",
                                 "type": "ToggleOff", "description": f"Stopped: {value}",
                                 "timestamp": time.time(),
                                 "time": time.strftime("%H:%M:%S"), "mapped": True})
            else:
                # Not running — start with stdin pipe for graceful stop
                # Use shell=True (not zsh -i) so stdin pipe works correctly
                print(f"  Toggle ON: starting '{value}'")
                expanded = os.path.expanduser(value)
                proc = subprocess.Popen(expanded, shell=True, stdin=subprocess.PIPE)
                toggle_processes[event_key] = proc
                broadcast_event({"key": event_key, "toggle_state": "on",
                                 "type": "ToggleOn", "description": f"Started: {value}",
                                 "timestamp": time.time(),
                                 "time": time.strftime("%H:%M:%S"), "mapped": True})


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------
def broadcast_event(event_data):
    """Send event to all SSE clients."""
    msg = f"data: {json.dumps(event_data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_queues.remove(q)


# ---------------------------------------------------------------------------
# CGEventTap callback
# ---------------------------------------------------------------------------
tap_ref = [None]  # store tap reference for re-enabling

def event_processor_worker():
    """Background thread that handles event buffering and SSE broadcast.

    Keeps the tap callback fast — it only checks mappings and returns."""
    while not shutdown_flag.is_set():
        try:
            item = event_process_queue.get(timeout=1)
        except queue.Empty:
            continue
        event_key, event_type, description, ts, action = item
        event_data = {
            "key": event_key,
            "type": EVENT_TYPE_NAMES.get(event_type, str(event_type)),
            "description": description,
            "timestamp": ts,
            "time": time.strftime("%H:%M:%S", time.localtime(ts)),
            "mapped": bool(action),
        }
        if action:
            event_data["mapping"] = action
        with event_buffer_lock:
            event_buffer.append(event_data)
            if len(event_buffer) > MAX_EVENTS:
                event_buffer.pop(0)
        broadcast_event(event_data)


def tap_callback(proxy, event_type, event, refcon):
    """Called for every input event. Must return quickly."""
    # If macOS disabled our tap (timeout), re-enable it immediately
    if event_type == 0xFFFFFFFE:  # kCGEventTapDisabledByTimeout
        print("  WARNING: Event tap was disabled by macOS — re-enabling")
        if tap_ref[0]:
            CGEventTapEnable(tap_ref[0], True)
        return event

    # Track modifier state from FlagsChanged events
    if event_type == kCGEventFlagsChanged:
        _update_mod_state(event)
        return event  # always pass through modifier events

    event_key = make_event_key(event_type, event)

    # Check for mapping — this is the only lock we need on the hot path
    with mappings_lock:
        action = mappings.get(event_key)

    # Queue description generation, buffering, and SSE broadcast for async processing
    description = describe_event(event_type, event)
    try:
        event_process_queue.put_nowait((event_key, event_type, description, time.time(), action))
    except queue.Full:
        pass  # drop event data rather than block the tap

    if action:
        # Execute mapped action in background
        action_with_key = dict(action, _event_key=event_key)
        threading.Thread(target=execute_action, args=(action_with_key,), daemon=True).start()
        # Always pass through mouse-up events — macOS needs them to terminate
        # drag/screenshot operations. Swallowing ANY mouse-up breaks screenshots.
        if event_type in (kCGEventLeftMouseUp, kCGEventRightMouseUp, kCGEventOtherMouseUp):
            return event
        return None  # swallow the original event
    return event


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MapperHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress request logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        # Serve UI
        if path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "ui.html not found")
            return

        # Serve mobile UI
        if path == "/mobile":
            try:
                with open(MOBILE_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "mobile.html not found")
            return

        # Device list
        if path == "/api/devices":
            self.send_json(devices)
            return

        # Mappings
        if path == "/api/mappings":
            with mappings_lock:
                self.send_json(mappings)
            return

        # Event buffer
        if path == "/api/events":
            with event_buffer_lock:
                self.send_json(list(event_buffer[-50:]))
            return

        # SSE stream
        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue(maxsize=100)
            with sse_lock:
                sse_queues.append(q)

            try:
                while not shutdown_flag.is_set():
                    try:
                        msg = q.get(timeout=1)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Send keepalive
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with sse_lock:
                    if q in sse_queues:
                        sse_queues.remove(q)
            return

        # Keycodes reference
        if path == "/api/keycodes":
            self.send_json(KEYCODE_NAMES)
            return

        # Voice intents
        if path == "/api/intents":
            self.send_json(voice_intents)
            return

        # Word corrections
        if path == "/api/corrections":
            # Normalize old string-format entries for the UI
            normalized = {}
            for k, v in word_corrections.items():
                if isinstance(v, str):
                    normalized[k] = {"to": v, "mode": "whole"}
                else:
                    normalized[k] = v
            self.send_json(normalized)
            return

        # Transcription log
        if path == "/api/transcriptions":
            with transcriptions_lock:
                self.send_json(list(transcriptions))
            return

        # Usage stats (aggregated from transcriptions)
        if path == "/api/usage":
            with usage_lock:
                self.send_json(dict(usage_stats))
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/mappings":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            event_key = body.get("event_key", "")
            if not event_key:
                self.send_json({"error": "event_key required"}, 400)
                return
            mapping = {
                "action_type": body.get("action_type", "block"),
                "action_value": body.get("action_value", ""),
                "label": body.get("label", ""),
            }
            with mappings_lock:
                mappings[event_key] = mapping
            save_config()
            self.send_json({"ok": True, "event_key": event_key, "mapping": mapping})
            return

        if path == "/api/intents":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            name = body.get("name", "").strip()
            if not name:
                self.send_json({"error": "name required"}, 400)
                return
            intent = {
                "name": name,
                "label": body.get("label", name),
                "prompt": body.get("prompt", ""),
                "model": body.get("model", "claude-haiku-4-5-20251001"),
                "output": body.get("output", "paste"),
                "context": bool(body.get("context", False)),
            }
            voice_intents[name] = intent
            save_config()
            self.send_json({"ok": True, "intent": intent})
            return

        # Word corrections — add or update
        if path == "/api/corrections":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            wrong = body.get("from", "").strip()
            correct = body.get("to", "").strip()
            mode = body.get("mode", "whole")
            if mode not in ("whole", "partial"):
                mode = "whole"
            if not wrong or not correct:
                self.send_json({"error": "both 'from' and 'to' are required"}, 400)
                return
            word_corrections[wrong.lower()] = {"to": correct, "mode": mode}
            save_config()
            self.send_json({"ok": True, "from": wrong.lower(), "to": correct, "mode": mode})
            return

        # Reprocess a transcription with a different intent
        # Combine multiple transcriptions into one
        if path == "/api/transcriptions/combine":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            ids = body.get("ids", [])
            if len(ids) < 2:
                self.send_json({"error": "need at least 2 ids"}, 400)
                return
            with transcriptions_lock:
                entries = [t for t in transcriptions if t["id"] in ids]
            # Sort by original order (by id)
            entries.sort(key=lambda t: t["id"])
            combined_raw = " ".join(e["raw"] for e in entries)
            # Create a new transcription entry
            entry = {
                "id": transcription_next_id[0],
                "timestamp": time.time(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "raw": combined_raw,
                "output": combined_raw,
                "intent": entries[0].get("intent", "raw"),
                "app": entries[0].get("app", ""),
                "duration": round(sum(e.get("duration", 0) for e in entries), 1),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0,
            }
            transcription_next_id[0] += 1
            with transcriptions_lock:
                transcriptions.append(entry)
                if len(transcriptions) > MAX_TRANSCRIPTIONS:
                    transcriptions[:] = transcriptions[-MAX_TRANSCRIPTIONS:]
            save_transcriptions()
            self.send_json({"ok": True, "transcription": entry})
            return

        if path == "/api/transcriptions/reprocess":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            tid = body.get("id")
            intent_name = body.get("intent", "raw")

            with transcriptions_lock:
                entry = next((t for t in transcriptions if t["id"] == tid), None)
            if not entry:
                self.send_json({"error": "transcription not found"}, 404)
                return

            # Allow edited raw text override
            raw = body.get("raw", entry["raw"])
            if raw != entry["raw"]:
                with transcriptions_lock:
                    for t in transcriptions:
                        if t["id"] == tid:
                            t["raw"] = raw
                            break
                save_transcriptions()
            intent = voice_intents.get(intent_name, DEFAULT_INTENTS.get(intent_name, {}))
            prompt = intent.get("prompt", "").strip()
            model = intent.get("model", "").strip()

            r_inp = 0
            r_out = 0
            r_cost = 0.0

            if prompt and model:
                try:
                    client = anthropic.Anthropic()
                    msg = client.messages.create(
                        model=model, max_tokens=1024,
                        system=prompt,
                        messages=[{"role": "user", "content": raw}]
                    )
                    output = msg.content[0].text
                    r_inp = msg.usage.input_tokens
                    r_out = msg.usage.output_tokens
                    r_cost = (r_inp * 0.80 + r_out * 4.00) / 1_000_000
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                    return
            else:
                output = raw

            # Update the entry in place (accumulate cost from reprocessing)
            with transcriptions_lock:
                for t in transcriptions:
                    if t["id"] == tid:
                        t["output"] = output
                        t["intent"] = intent_name
                        t["input_tokens"] = t.get("input_tokens", 0) + r_inp
                        t["output_tokens"] = t.get("output_tokens", 0) + r_out
                        t["cost"] = round(t.get("cost", 0) + r_cost, 6)
                        updated = dict(t)
                        break
            save_transcriptions()
            if r_cost > 0:
                accumulate_usage(intent_name, r_inp, r_out, r_cost, 0)
            self.send_json({"ok": True, "transcription": updated})
            return

        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        if path.startswith("/api/mappings/"):
            event_key = path[len("/api/mappings/"):]
            from urllib.parse import unquote
            event_key = unquote(event_key)
            with mappings_lock:
                removed = mappings.pop(event_key, None)
            if removed:
                save_config()
                self.send_json({"ok": True, "removed": event_key})
            else:
                self.send_json({"error": "not found"}, 404)
            return

        if path.startswith("/api/intents/"):
            from urllib.parse import unquote
            intent_name = unquote(path[len("/api/intents/"):])
            removed = voice_intents.pop(intent_name, None)
            if removed:
                save_config()
                self.send_json({"ok": True, "removed": intent_name})
            else:
                self.send_json({"error": "not found"}, 404)
            return

        # Delete word correction
        if path.startswith("/api/corrections/"):
            from urllib.parse import unquote
            wrong = unquote(path[len("/api/corrections/"):])
            removed = word_corrections.pop(wrong, None)
            if removed is not None:
                save_config()
                self.send_json({"ok": True, "removed": wrong})
            else:
                self.send_json({"error": "not found"}, 404)
            return

        # Delete single transcription
        if path.startswith("/api/transcriptions/"):
            try:
                tid = int(path[len("/api/transcriptions/"):])
            except ValueError:
                self.send_json({"error": "invalid id"}, 400)
                return
            with transcriptions_lock:
                before = len(transcriptions)
                transcriptions[:] = [t for t in transcriptions if t["id"] != tid]
                removed = len(transcriptions) < before
            if removed:
                save_transcriptions()
                self.send_json({"ok": True, "removed": tid})
            else:
                self.send_json({"error": "not found"}, 404)
            return

        # Clear all transcriptions
        if path == "/api/transcriptions":
            with transcriptions_lock:
                transcriptions.clear()
            save_transcriptions()
            self.send_json({"ok": True})
            return

        self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    port = 9876
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    print("=" * 50)
    print("  INPUT MAPPER")
    print("=" * 50)
    print()

    # Load config
    print("[config]")
    load_config()
    load_transcriptions()
    load_usage()

    # Preload voice pipeline model
    print("\n[voice pipeline]")
    vp_load_model()

    # Enumerate devices
    print("\n[devices]")
    enumerate_devices()

    # Create event tap
    print("\n[event tap]")
    event_mask = (
        CGEventMaskBit(kCGEventLeftMouseDown)
        | CGEventMaskBit(kCGEventRightMouseDown)
        | CGEventMaskBit(kCGEventOtherMouseDown)
        | CGEventMaskBit(kCGEventLeftMouseUp)
        | CGEventMaskBit(kCGEventRightMouseUp)
        | CGEventMaskBit(kCGEventOtherMouseUp)
        | CGEventMaskBit(kCGEventKeyDown)
        | CGEventMaskBit(kCGEventScrollWheel)
        | CGEventMaskBit(kCGEventFlagsChanged)
    )

    tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        event_mask,
        tap_callback,
        None,
    )

    if tap is None:
        print("  FAILED — Accessibility permission not granted")
        print("  Fix: System Settings > Privacy & Security > Accessibility")
        sys.exit(1)

    print("  CGEventTap created: OK")

    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = CFRunLoopGetCurrent()
    run_loop_ref[0] = loop
    CFRunLoopAddSource(loop, source, kCFRunLoopCommonModes)
    tap_ref[0] = tap
    CGEventTapEnable(tap, True)

    # Start async event processor (keeps tap callback fast)
    proc_thread = threading.Thread(target=event_processor_worker, daemon=True)
    proc_thread.start()

    # Start HTTP server
    print(f"\n[server]")
    server = ThreadedHTTPServer(("0.0.0.0", port), MapperHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"  UI: http://localhost:{port}")

    # Signal handler
    def handle_shutdown(sig, frame):
        print("\n\nShutting down...")
        shutdown_flag.set()
        server.shutdown()
        if run_loop_ref[0]:
            CFRunLoopStop(run_loop_ref[0])

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    print(f"\n  Ready. Press Ctrl+C to stop.\n")

    # Auto-open UI in browser
    import webbrowser
    webbrowser.open(f"http://localhost:{port}")

    # Notify
    try:
        subprocess.run(["cmux", "notify", "--title", "input-mapper",
                        "--body", f"Running on localhost:{port}"],
                       capture_output=True, timeout=3)
    except Exception:
        pass

    # Run the CFRunLoop on the main thread (required for CGEventTap)
    CFRunLoopRun()


if __name__ == "__main__":
    main()
