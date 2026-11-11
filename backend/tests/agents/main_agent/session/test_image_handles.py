"""Image handles in the persisted message (unified-attachment-delivery §6.2, phase 4a).

Pins: an attached image the turn sent from a stored copy is persisted as a
handle naming that copy (variant, bucket, key), matched by size, head and tail;
an image with no stored copy keeps its bytes; and restore reads the stored copy
back so the restored history is **byte-identical** to the history restored from
the bytes, through the SDK's own Memory encoding. A foreign key, a missing
object or a size change becomes a short note, never a crash.
"""

from __future__ import annotations

import base64
import json
import random
from typing import Any, Dict, List

import pytest
from strands.types.session import SessionMessage

from agents.main_agent.session import image_restore as ir
from apis.shared.files import attachment_handle as ah
from apis.shared.sessions.messages import _ensure_image_base64

USER = "user-1"
SESSION = "s1"
BUCKET = "files-bucket"


def _bytes(n: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


HIGH = _bytes(3_000, 1)
COMPACT = _bytes(1_200, 2)
LEGACY = _bytes(900, 3)


def _ref(upload_id, raw, variant="high", key=None):
    size, head, tail = ah.image_fingerprint_b64(base64.b64encode(raw).decode())
    return ah.ImageRef(
        upload_id=upload_id, variant=variant, mime_type="image/jpeg", bucket=BUCKET,
        key=key or f"user-files/{USER}/{SESSION}/{upload_id}/_derived/model-image.jpg",
        size_bytes=size, head=head, tail=tail,
    )


def _img(raw, fmt="jpeg"):
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


def _round_trip(message: Dict[str, Any]) -> Dict[str, Any]:
    stored = json.loads(json.dumps(SessionMessage.from_message(message, 0).to_dict()))
    return SessionMessage.from_dict(stored).to_message()


@pytest.fixture
def store(monkeypatch):
    objects: Dict[tuple, bytes] = {}
    reads: List[str] = []

    def _read(bucket, key):
        reads.append(key)
        return objects[(bucket, key)]

    monkeypatch.setattr(ir, "_read", _read)
    return objects, reads


def test_the_fingerprint_reads_the_ends_without_decoding_the_body():
    for raw in (HIGH, COMPACT, b"x", b"xy", b"xyz", _bytes(47, 9)):
        size, head, tail = ah.image_fingerprint_b64(base64.b64encode(raw).decode())
        assert (size, head, tail) == (len(raw), raw[:48], raw[-48:])


def test_a_sent_copy_becomes_a_handle_and_the_live_message_keeps_its_bytes():
    live = {"role": "user", "content": [{"text": "what is this?"}, _img(HIGH), _img(COMPACT, "webp")]}
    refs = [_ref("up-a", HIGH), _ref("up-b", COMPACT, variant="compact")]
    persisted = ah.persisted_message(live, lambda *_: None, refs)

    assert live["content"][1]["image"]["source"]["bytes"] == HIGH
    a = ah.handle_of(persisted["content"][1]["image"])
    b = ah.handle_of(persisted["content"][2]["image"])
    assert (a["uploadId"], a["variant"], a["sizeBytes"], a["class"]) == ("up-a", "high", len(HIGH), "image")
    assert (b["uploadId"], b["variant"]) == ("up-b", "compact")
    assert persisted["content"][2]["image"]["format"] == "webp"
    assert HIGH not in repr(persisted).encode()


def test_an_image_with_no_stored_copy_keeps_its_bytes():
    live = {"role": "user", "content": [_img(LEGACY)]}
    assert ah.persisted_message(live, lambda *_: None, [_ref("up-a", HIGH)]) is live


def test_two_identical_images_claim_two_refs():
    live = {"role": "user", "content": [_img(HIGH), _img(HIGH)]}
    persisted = ah.persisted_message(live, lambda *_: None, [_ref("up-2", HIGH), _ref("up-1", HIGH)])
    ids = [ah.handle_of(b["image"])["uploadId"] for b in persisted["content"]]
    assert ids == ["up-2", "up-1"]


def test_restore_is_byte_identical_to_restoring_the_bytes(store):
    objects, reads = store
    refs = [_ref("up-a", HIGH), _ref("up-b", COMPACT, variant="compact")]
    for r, raw in zip(refs, (HIGH, COMPACT)):
        objects[(BUCKET, r.key)] = raw
    live = [
        {"role": "user", "content": [{"text": "compare"}, _img(HIGH), _img(COMPACT, "webp")]},
        {"role": "assistant", "content": [{"text": "They differ."}]},
    ]
    with_bytes = [_round_trip(m) for m in live]
    with_handles = [_round_trip(ah.persisted_message(m, lambda *_: None, refs)) for m in live]

    result = ir.restore_image_handles(with_handles, user_id=USER)

    assert json.dumps(result.messages, default=repr, sort_keys=True) == json.dumps(with_bytes, default=repr, sort_keys=True)
    assert (result.restored, result.failed, result.bytes_read) == (2, 0, len(HIGH) + len(COMPACT))
    assert sorted(reads) == sorted(r.key for r in refs)


def test_a_history_without_handles_reads_nothing(store):
    _, reads = store
    messages = [{"role": "user", "content": [_img(HIGH)]}]
    result = ir.restore_image_handles(messages, user_id=USER)
    assert result.messages is messages and reads == []


@pytest.mark.parametrize("case", ["foreign_key", "missing", "resized", "no_user"])
def test_an_unreadable_copy_becomes_a_note(store, case):
    objects, reads = store
    key = f"user-files/someone-else/{SESSION}/up-a/x.jpg" if case == "foreign_key" else None
    ref = _ref("up-a", HIGH, key=key)
    if case == "resized":
        objects[(BUCKET, ref.key)] = HIGH + b"!"
    message = _round_trip(ah.persisted_message({"role": "user", "content": [_img(HIGH)]}, lambda *_: None, [ref]))
    result = ir.restore_image_handles([message], user_id=None if case == "no_user" else USER)
    assert result.failed == 1
    assert result.messages[0]["content"][0] == {"text": "[Image unavailable: upload_id=up-a]"}
    if case in ("foreign_key", "no_user"):
        assert reads == [], "a key outside the user's prefix is never read"


def test_restore_never_touches_its_input(store):
    objects, _ = store
    ref = _ref("up-a", HIGH)
    objects[(BUCKET, ref.key)] = HIGH
    message = ah.persisted_message({"role": "user", "content": [_img(HIGH)]}, lambda *_: None, [ref])
    before = repr(message)
    ir.restore_image_handles([message], user_id=USER)
    assert repr(message) == before


def test_the_registry_is_per_user_and_session():
    ah.clear_session_uploads()
    ah.remember_turn_images(USER, SESSION, [_ref("up-a", HIGH)])
    assert [r.upload_id for r in ah.turn_images(USER, SESSION)] == ["up-a"]
    assert ah.turn_images("other", SESSION) == []
    ah.clear_session_uploads()


def test_the_message_api_shows_an_image_handle_as_its_format():
    persisted = ah.persisted_message({"role": "user", "content": [_img(HIGH)]}, lambda *_: None, [_ref("up-a", HIGH)])
    assert _ensure_image_base64(persisted["content"][0]["image"]) == {"format": "jpeg"}


def test_the_route_names_the_copy_each_image_was_sent_from(monkeypatch):
    from apis.inference_api.chat import routes
    from apis.inference_api.chat.models import FileContent
    from apis.shared.files.models import FileMetadata, FileStatus

    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "true")
    ah.clear_session_uploads()
    model_image = {
        "version": 1, "key": f"user-files/{USER}/{SESSION}/up-a/_derived/model-image.jpg", "mimeType": "image/jpeg",
        "compact": {"key": f"user-files/{USER}/{SESSION}/up-a/_derived/model-image-compact.webp", "mimeType": "image/webp"},
    }
    row = FileMetadata(
        upload_id="up-a", user_id=USER, session_id=SESSION, filename="photo.png", mime_type="image/png",
        size_bytes=9_000, s3_key=f"user-files/{USER}/{SESSION}/up-a/photo.png", s3_bucket=BUCKET,
        status=FileStatus.READY, model_image=model_image,
    )
    sent = FileContent(filename="photo.png", content_type="image/webp", bytes=base64.b64encode(COMPACT).decode())
    attachments = routes.TurnAttachments(files_to_send=[sent])
    attachments.upload_rows[id(sent)] = row
    attachments.compact_image_ids = {id(sent)}

    routes._remember_sent_images(attachments, USER, SESSION)

    [ref] = ah.turn_images(USER, SESSION)
    assert (ref.variant, ref.key, ref.mime_type, ref.size_bytes) == (
        "compact", model_image["compact"]["key"], "image/webp", len(COMPACT),
    )
    assert ref.matches(COMPACT)
    ah.clear_session_uploads()


def test_the_route_remembers_nothing_under_the_legacy_profile(monkeypatch):
    from apis.inference_api.chat import routes
    from apis.inference_api.chat.models import FileContent

    monkeypatch.delenv("ATTACHMENT_PLAN_ENABLED", raising=False)
    ah.clear_session_uploads()
    sent = FileContent(filename="photo.png", content_type="image/png", bytes=base64.b64encode(HIGH).decode())
    attachments = routes.TurnAttachments(files_to_send=[sent])
    attachments.upload_rows[id(sent)] = object()
    routes._remember_sent_images(attachments, USER, SESSION)
    assert ah.turn_images(USER, SESSION) == []
