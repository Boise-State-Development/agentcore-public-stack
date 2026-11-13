---
title: Voice Mode
description: Spoken conversations with Amazon Nova 2 Sonic, and dictation into the composer with Amazon Transcribe.
sidebar:
  order: 6
---

The composer has two speech features, and they do different jobs:

| Feature | What you get | Service | Control in the composer |
|---|---|---|---|
| **Voice mode** | A spoken conversation: you talk, the assistant answers out loud | Amazon Nova 2 Sonic (`amazon.nova-2-sonic-v1:0`) | The waveform button, with a chevron for options |
| **Dictation** | Your speech typed into the message box, which you then edit and send | Amazon Transcribe Streaming | The **Dictate** (microphone) button |

Dictation only fills in text; nothing it produces reaches the model unless you
send it. Voice mode is a separate, real-time conversation with a speech model.

## Voice mode

Click the waveform button in the composer to start. A **Voice Mode** panel
opens with a live transcript of both sides; you can interrupt the assistant by
talking over it. **End Voice Session** closes it. The session also ends by
itself after 60 seconds of silence.

The chevron beside the button opens **Voice mode options**:

- **Voice:** all 16 Nova 2 Sonic voices, grouped by language (English US, UK,
  Australia and India, Hindi, French, Italian, German, Spanish US and
  Portuguese Brazil). The default is Tiffany. Tiffany and Matthew are the two
  voices that speak every supported language, so they keep the same voice if
  you switch language mid-conversation. Your choice is saved to your account
  as the `voiceId` user setting.
- **Microphone:** which input device listens. This is stored per browser and
  shared with dictation. A device that has disappeared falls back to the
  system default.
- **Start voice conversation.**

Choices are fixed when a session connects, so the chevron is hidden while one
is live.

### What the voice agent knows and can do

- **Conversation history.** Voice runs inside the current conversation. It
  loads the conversation's recent text history (the last 20 messages by
  default, `NOVA_SONIC_MAX_MESSAGES`), so you can switch from typing to talking
  and keep context. A voice session appears in the conversation list like any
  other.
- **Long-term memory.** Your memory setting applies. In an incognito chat,
  memory is read-only.
- **Tools.** Voice does not use the composer's tool selection. It gets the
  tools an admin has pinned always-on and the platform's system tools, filtered
  by your roles, minus any tool that is disabled.
- **Instructions.** Your role instructions and personal instructions apply,
  in the same order and with the same precedence as a text chat. They are
  read once, when the voice session connects.
- **Prompt.** After those, the system prompt adds short voice guidelines (be
  concise, avoid lists and heavy formatting) and one line naming the voice, so
  languages that mark the speaker's gender use the matching form.
- **Not applied:** the active conversation mode, and the chat model picker.
  Voice always runs on the Nova 2 Sonic model (`NOVA_SONIC_MODEL_ID` on the
  Runtime overrides the id).

Voice is not offered inside the Agent Designer's preview or the marketplace
review test drive.

### How it is reached

Voice mode has no feature flag; it is available to any signed-in user. The
connection is a WebSocket, which the browser cannot authenticate with the
normal request middleware, so it works in two steps through the App API:

1. **`POST /voice/ticket`** runs through the usual session cookie and CSRF
   checks and returns a signed, single-use ticket bound to you and the
   conversation. It is valid for 60 seconds.
2. **`WebSocket /voice/stream?ticket=…`** checks, before accepting, that the
   request's `Origin` is in `CORS_ORIGINS`, that the session cookie is valid,
   and that the ticket is genuine, unexpired, unused, for voice, and issued to
   the same user. A ticket can open only its own kind of socket.

The App API then opens the upstream WebSocket to the AgentCore Runtime with
the Cognito access token it holds for your session, and relays frames in both
directions. The browser never holds a Cognito token. In the Runtime, the
voice route takes your identity only from the token on that upgrade; a user id
sent anywhere else must match it, or the socket closes with code `4001`. A
conversation that belongs to another user closes with `4004`.

The ticket signing secret (`VOICE_TICKET_SIGNING_SECRET_ARN`) and the replay
table are part of the stack. If the signing secret is not configured, the
ticket call returns `503 Voice service is not configured`.

### Pricing

Voice is priced from the model catalog row for `amazon.nova-2-sonic-v1:0`,
curated as **Nova 2 Sonic** on the Bedrock tab of
[Admin → Models](/agentcore-public-stack/admin/models/). Nova 2 Sonic bills
speech tokens at a much higher rate than text tokens, so the row carries both:
the `input` and `output` fields are the text rates, and the speech input and
output fields are the speech rates. Each voice session's usage is billed per
bucket and recorded on the conversation's last assistant message, so it counts
against the user's quota.

**Nothing seeds that row.** Until an admin adds it in each environment, voice
sessions price to nothing, count $0 against quota, and fire the
`UnmeteredModelCall` metric and its alarm. The row does not need to be enabled
for chat: speech models are hidden from the chat model picker.

## Dictation

Click **Dictate** and speak. A live transcript appears in italics at the
caret.

- **Done** (or Enter) inserts the transcript into your draft without sending.
- **Cancel** (or Escape) restores the draft exactly as it was.
- Tones mark when the microphone goes live and when it stops.

One dictation is capped at five minutes, so a forgotten open microphone cannot
keep billing.

Dictation uses the same ticket pattern as voice: `POST /dictation/ticket`,
then `WebSocket /dictation/stream`. The ticket is bound to you alone, because
you can dictate into a new conversation before it exists. The App API presigns
the Transcribe Streaming connection with its own task role
(`transcribe:StartStreamTranscriptionWebSocket`) and relays audio up and text
back, so the browser never talks to AWS directly.

### Turning dictation off, and languages

Dictation is a default-on feature switch:

| Setting | Where | Default | Effect |
|---|---|---|---|
| `DICTATION_ENABLED` | App API, from `CDK_DICTATION_ENABLED` | on (`!= "false"`) | `false` makes both dictation routes 404. The SPA hides the Dictate button on the first 404 for the rest of the tab session |
| `DICTATION_LANGUAGES` | App API, from `CDK_DICTATION_LANGUAGES` | `en-US` | A comma-separated list of Transcribe language codes. One code pins the language; two or more turn on automatic language identification |

There is no SPA `features.*` flag for dictation. Transcribe audio minutes are
their own line on the AWS bill. Before a wide rollout, check the account's
Transcribe concurrent-stream quota; the
[AWS Limits](/agentcore-public-stack/admin/aws-limits/) page lists it while
dictation is on.
