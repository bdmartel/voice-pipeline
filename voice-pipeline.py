#!/usr/bin/env python3
"""Voice-to-prompt pipeline. Speak → transcribe → rewrite → clipboard.

Usage:
    vp              Auto-stops after 2s of silence
    vp --enter      Manual stop (press Enter)
    vp --raw        Skip rewrite, always output raw transcription
"""

import sys
import wave
import time
import tempfile
import subprocess
import threading
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import anthropic

SAMPLE_RATE = 16000
CHANNELS = 1
SILENCE_THRESHOLD = 500    # RMS below this = silence
SILENCE_DURATION = 2.0     # seconds of silence before auto-stop
MIN_RECORDING = 0.5        # minimum seconds before silence detection kicks in

REWRITE_PROMPT = """You are a prompt translator. The user dictated something out loud and it was
transcribed by speech-to-text. Your job is to convert this raw transcription into a clean,
actionable prompt ready to paste into a coding assistant (Claude Code in a terminal).

Rules:
- Capture the user's INTENT, not their literal words
- Strip filler words, false starts, repetition
- If they're describing a task, make it a clear instruction
- If they're asking a question, make it a precise question
- Keep their technical terms and specifics intact
- Output ONLY the cleaned prompt, nothing else — no quotes, no preamble
- Match the complexity of the output to the input — a simple request stays simple"""

# Apps that get the rewrite treatment — everything else gets raw transcription
REWRITE_APPS = {"Ghostty", "Terminal", "iTerm2"}


def get_frontmost_app():
    """Get the name of the frontmost macOS application."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2)
        return result.stdout.strip()
    except Exception:
        return ""


def notify(title, body):
    """Send a cmux notification (silent fallback if cmux unavailable)."""
    try:
        subprocess.run(["cmux", "notify", "--title", title, "--body", body],
                       capture_output=True, timeout=3)
    except Exception:
        pass


def record_audio_silence():
    """Record until silence is detected. Returns numpy array."""
    frames = []
    stop = threading.Event()
    silence_start = [None]
    recording_start = [time.time()]

    def callback(indata, frame_count, time_info, status):
        if stop.is_set():
            return
        frames.append(indata.copy())

        elapsed = time.time() - recording_start[0]
        if elapsed < MIN_RECORDING:
            return

        rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
        if rms < SILENCE_THRESHOLD:
            if silence_start[0] is None:
                silence_start[0] = time.time()
            elif time.time() - silence_start[0] >= SILENCE_DURATION:
                stop.set()
        else:
            silence_start[0] = None

    print("🎙  Recording... (auto-stops after 2s silence)")
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype='int16', callback=callback)
    stream.start()
    while not stop.is_set():
        time.sleep(0.1)
    stream.stop()
    stream.close()

    if not frames:
        return None
    return np.concatenate(frames)


def record_audio_enter():
    """Record until Enter is pressed. Returns numpy array."""
    frames = []
    stop = threading.Event()

    def callback(indata, frame_count, time_info, status):
        if not stop.is_set():
            frames.append(indata.copy())

    print("🎙  Recording... press ENTER to stop")
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype='int16', callback=callback)
    stream.start()
    input()
    stop.set()
    stream.stop()
    stream.close()

    if not frames:
        return None
    return np.concatenate(frames)


def preprocess_audio(audio):
    """Clean audio for better transcription accuracy."""
    audio_float = audio.astype(np.float32).flatten()
    # Remove DC offset (kills low-frequency mic bias)
    audio_float -= np.mean(audio_float)
    # Peak normalization — use full dynamic range so Whisper gets a strong signal
    peak = np.max(np.abs(audio_float))
    if peak > 0:
        audio_float = audio_float / peak * 32000
    return audio_float.astype(np.int16)


def save_wav(audio, path):
    """Write numpy audio array to wav file."""
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())


def transcribe(wav_path):
    """Transcribe wav file with faster-whisper."""
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav_path)
    return " ".join(seg.text.strip() for seg in segments)


def rewrite(raw_text):
    """Rewrite raw transcription into a clean prompt via Claude API."""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=REWRITE_PROMPT,
        messages=[{"role": "user", "content": raw_text}]
    )
    # Haiku 4.5: $0.80/M input, $4.00/M output
    inp = msg.usage.input_tokens
    out = msg.usage.output_tokens
    cost = (inp * 0.80 + out * 4.00) / 1_000_000
    print(f"   💰 {inp}+{out} tokens = ${cost:.4f}")
    return msg.content[0].text


def to_clipboard(text):
    """Copy text to macOS clipboard."""
    subprocess.run(["pbcopy"], input=text.encode(), check=True)


def main():
    args = sys.argv[1:]
    use_enter = "--enter" in args
    force_raw = "--raw" in args

    # Record
    if use_enter:
        audio = record_audio_enter()
    else:
        audio = record_audio_silence()

    if audio is None or len(audio) == 0:
        print("No audio captured.")
        return

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    audio = preprocess_audio(audio)
    save_wav(audio, wav_path)

    duration = len(audio) / SAMPLE_RATE
    print(f"   {duration:.1f}s captured")

    # Detect frontmost app
    app = get_frontmost_app()
    use_rewrite = not force_raw and app in REWRITE_APPS

    # Transcribe
    print("📝 Transcribing...")
    raw = transcribe(wav_path)
    if not raw.strip():
        print("No speech detected.")
        return
    print(f"   Raw: {raw}")

    if use_rewrite:
        print(f"✨ Rewriting (detected: {app})...")
        output = rewrite(raw)
    else:
        if force_raw:
            print("   Passthrough (--raw)")
        else:
            print(f"   Passthrough (detected: {app})")
        output = raw

    print(f"\n   {output}\n")

    # Clipboard + notify
    to_clipboard(output)
    print("📋 Copied to clipboard.")
    notify("vp", output[:80])


if __name__ == "__main__":
    main()
