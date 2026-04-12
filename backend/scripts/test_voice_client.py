#!/usr/bin/env python3
"""
Minimal WebSocket test client for the voice endpoint.

Usage:
    # With a real JWT token (from Cognito):
    python scripts/test_voice_client.py --token "eyJhbGc..."

    # With a fake token for local testing (requires no auth verification):
    python scripts/test_voice_client.py --fake-token

    # Send a text message instead of audio (simpler test):
    python scripts/test_voice_client.py --fake-token --text "Hello, what can you help me with?"

Prerequisites:
    pip install websockets
    # Server must be running: cd src/apis/inference_api && uv run python main.py
"""

import argparse
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    sys.exit(1)

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None


def make_fake_token() -> str:
    """Create a fake JWT token for local testing."""
    if pyjwt is None:
        print("pyjwt not available for fake token generation, using placeholder")
        return "fake.token.placeholder"
    return pyjwt.encode(
        {"sub": "test-user-001", "email": "test@example.com", "name": "Test User"},
        "test-secret",
        algorithm="HS256",
    )


async def run_client(url: str, token: str, text_message: str = None):
    """Connect to voice WebSocket and exchange messages."""
    print(f"Connecting to {url}")

    async with websockets.connect(url) as ws:
        print("Connected!")

        # Send config message
        config = {
            "type": "config",
            "session_id": "voice-test-001",
            "auth_token": token,
        }
        await ws.send(json.dumps(config))
        print(f"Sent config: {json.dumps(config, indent=2)}")

        # Read connection confirmation
        response = await asyncio.wait_for(ws.recv(), timeout=15.0)
        print(f"Received: {response}")

        if text_message:
            # Send a text message (simpler than audio for testing)
            msg = {"type": "bidi_text_input", "text": text_message}
            await ws.send(json.dumps(msg))
            print(f"Sent text: {text_message}")

            # Read responses for a few seconds
            print("\nListening for responses (10s timeout)...")
            try:
                while True:
                    response = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    try:
                        parsed = json.loads(response)
                        print(f"  Event: {json.dumps(parsed, indent=2)[:200]}")
                    except json.JSONDecodeError:
                        print(f"  Raw: {response[:200]}")
            except asyncio.TimeoutError:
                print("No more responses (timeout)")
        else:
            # Just test ping/pong
            await ws.send(json.dumps({"type": "ping"}))
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"Ping response: {response}")

        # Send stop
        await ws.send(json.dumps({"type": "stop"}))
        print("Sent stop signal")


def main():
    parser = argparse.ArgumentParser(description="Test voice WebSocket endpoint")
    parser.add_argument("--url", default="ws://localhost:8001/voice/stream", help="WebSocket URL")
    parser.add_argument("--token", help="JWT bearer token")
    parser.add_argument("--fake-token", action="store_true", help="Generate a fake JWT for local testing")
    parser.add_argument("--text", help="Send a text message instead of audio")
    args = parser.parse_args()

    token = args.token
    if args.fake_token:
        token = make_fake_token()
    if not token:
        print("Error: Provide --token or --fake-token")
        sys.exit(1)

    url = f"{args.url}?session_id=voice-test-001&token={token}"

    try:
        asyncio.run(run_client(url, token, args.text))
    except ConnectionRefusedError:
        print(f"Connection refused. Is the server running at {args.url}?")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted")


if __name__ == "__main__":
    main()
