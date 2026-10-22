"""Composer dictation — speech-to-text into the message box.

  1. ``POST /dictation/ticket``  — CSRF-protected; a single-use ticket bound to
                                  the user, ``purpose="dictation"``.
  2. ``WebSocket /dictation/stream`` — verifies the ticket, presigns an Amazon
                                  Transcribe Streaming WebSocket with the task
                                  role, and relays PCM up / transcripts down.

Nothing here reaches the model: the text lands in the composer, and only what
the user then sends becomes a message.
"""

from .routes import router

__all__ = ["router"]
