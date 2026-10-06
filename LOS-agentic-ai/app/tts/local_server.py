"""
A LOCAL, OFFLINE TTS SERVICE for development and on-prem use (2026-10-06).

    python -m app.tts.local_server [--port 5002]
    then set TTS_PROVIDER_URL=http://127.0.0.1:5002

Speaks with the voices INSTALLED ON THIS MACHINE through the Windows speech
engine (SAPI, via pywin32's win32com -- already a dependency of the environment).
No cloud call, no paid API; case text never leaves the machine.

API (what app/tts/service.py calls):  POST /synthesize  {"text", "voice", "language"}
  200 audio/wav          the spoken text
  404 {"error": ...}     no installed voice matches `voice` -- never a substitute voice:
                         Marathi text read by an English voice is wrong, not degraded.
  GET /voices            the installed voices, so configuration can be checked.

Any other engine (AI4Bharat Indic-TTS, Piper) can stand behind the same API.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()              # SAPI is not re-entrant: one synthesis at a time
MAX_CHARS = 2000


def installed_voices() -> list[str]:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        return [voice.GetVoices().Item(i).GetDescription() for i in range(voice.GetVoices().Count)]
    finally:
        pythoncom.CoUninitialize()


def synthesize(text: str, voice_name: str) -> bytes:
    """WAV bytes for `text` in the installed voice whose name contains `voice_name`."""
    import pythoncom
    import win32com.client

    with _LOCK:
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = speaker.GetVoices()
            match = next((tokens.Item(i) for i in range(tokens.Count)
                          if voice_name.lower() in tokens.Item(i).GetDescription().lower()), None)
            if match is None:
                raise LookupError(f"no installed voice matches '{voice_name}'")
            speaker.Voice = match
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Open(path, 3)                       # SSFMCreateForWrite
                speaker.AudioOutputStream = stream
                speaker.Speak(text[:MAX_CHARS])
                stream.Close()
                with open(path, "rb") as f:
                    return f.read()
            finally:
                os.unlink(path)
        finally:
            pythoncom.CoUninitialize()


class _Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/voices":
            self._json(200, {"voices": installed_voices()})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/synthesize":
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            text, voice = str(body.get("text") or "").strip(), str(body.get("voice") or "").strip()
            if not text or not voice:
                return self._json(400, {"error": "text and voice are required"})
            audio = synthesize(text, voice)
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a failed synthesis is a 500, never a crash
            return self._json(500, {"error": type(exc).__name__})
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, *_args):   # the spoken text is case data: never logged
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    args = parser.parse_args()
    print(f"Local TTS on http://{args.host}:{args.port}  voices: {installed_voices()}")
    ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
