"""
Text to speech.

ElevenLabs when a key is present, browser speechSynthesis otherwise.

The key stays here, server-side. The browser asks /api/speak for audio and gets
mp3 bytes back — it never sees the key, so nothing leaks into devtools, the
page source, or a screen recording.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Rachel — calm, British-adjacent. Override with ELEVENLABS_VOICE_ID.
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"
MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")


def available():
    return bool(os.environ.get("ELEVENLABS_API_KEY"))


def voice_id():
    return os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)


def speak_stream(text, chunk_size=4096):
    """Yields mp3 bytes chunks from ElevenLabs low-latency streaming endpoint, or raises."""
    if not available():
        raise RuntimeError("no ELEVENLABS_API_KEY")
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")

    qs = urllib.parse.urlencode({
        "optimize_streaming_latency": 3,
        "output_format": "mp3_44100_128",
    })
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id()}/stream?{qs}",
        data=json.dumps({
            "text": text[:2500],
            "model_id": MODEL,
            "voice_settings": {
                "stability": 0.42,          # a little variation; 0.5+ goes flat
                "similarity_boost": 0.85,
                "style": 0.25,
                "use_speaker_boost": True,
            },
        }).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "audio/mpeg",
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        while True:
            chunk = r.read(chunk_size)
            if not chunk:
                break
            yield chunk


def speak(text):
    """Returns mp3 bytes, or raises. Caller decides what to do on failure."""
    return b"".join(speak_stream(text))


def transcribe(audio, mime="audio/webm"):
    """Speech to text via ElevenLabs Scribe. Returns the transcript, or raises.

    Used instead of the browser's Web Speech API because that one only exists in
    Chrome and is a silently-failing stub in Brave — and because it ships your
    audio to Google. This keeps listening on the same provider as speaking.
    """
    if not available():
        raise RuntimeError("no ELEVENLABS_API_KEY")
    if not audio:
        raise ValueError("empty audio")

    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
           "audio/mpeg": "mp3", "audio/wav": "wav"}.get(mime.split(";")[0], "webm")
    boundary = "----jarvis" + os.urandom(8).hex()
    b = boundary.encode()

    body = b"".join([
        b"--", b, b"\r\n",
        b'Content-Disposition: form-data; name="model_id"\r\n\r\nscribe_v1\r\n',
        b"--", b, b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="turn.{ext}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        audio, b"\r\n",
        b"--", b, b"--\r\n",
    ])

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text",
        data=body, method="POST",
        headers={
            "content-type": f"multipart/form-data; boundary={boundary}",
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return (json.loads(r.read()).get("text") or "").strip()


def voices():
    """List the account's voices, so you can find an id to put in .env."""
    if not available():
        return []
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v2/voices",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
    except Exception:                                  # noqa: BLE001
        return []
    return [dict(id=v.get("voice_id"), name=v.get("name"),
                 labels=v.get("labels", {}))
            for v in body.get("voices", [])]
