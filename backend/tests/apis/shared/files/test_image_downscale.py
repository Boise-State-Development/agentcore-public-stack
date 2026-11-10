"""The model copy of an image (``apis.shared.files.image_downscale``, spec §4.3)."""

from __future__ import annotations

import hashlib
import io
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from PIL import Image

from apis.shared.files import image_downscale as d


def _noise(w: int, h: int, mode: str = "RGB", seed: int = 7) -> Image.Image:
    rnd = random.Random(seed)
    channels = len(mode)
    return Image.frombytes(mode, (w, h), rnd.randbytes(w * h * channels))


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _jpeg(w: int, h: int, orientation: int | None = None, quality: int = 95) -> bytes:
    kw = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kw["exif"] = exif.tobytes()
    return _encode(_noise(w, h), "JPEG", quality=quality, **kw)


class TestTargetSize:
    @pytest.mark.parametrize("w,h", [(8000, 6000), (6000, 4000), (4000, 4000), (12000, 1000), (3000, 3000), (2600, 100)])
    def test_inside_both_limits_and_the_many_image_limit(self, w, h):
        tw, th = d.target_size(w, h)
        assert max(tw, th) <= d.MAX_LONG_EDGE_PX
        assert d.visual_tokens(tw, th) <= d.MAX_VISUAL_TOKENS
        # Anthropic's stricter per-image limit above 20 image/document blocks.
        assert max(tw, th) <= 3000
        # Aspect ratio kept to within a pixel of rounding.
        assert abs(tw / th - w / h) < 0.02 * (w / h)

    def test_is_the_largest_size_that_fits(self):
        tw, th = d.target_size(6000, 4000)
        # One percent larger would no longer fit.
        assert d.visual_tokens(int(tw * 1.01), int(th * 1.01)) > d.MAX_VISUAL_TOKENS

    def test_never_upscales(self):
        assert d.target_size(1000, 800) == (1000, 800)
        assert d.target_size(1, 1) == (1, 1)

    def test_visual_tokens_is_one_per_28px_patch(self):
        assert d.visual_tokens(1000, 1000) == 1296
        assert d.visual_tokens(1920, 1080) == 2691


class TestPlanModelImage:
    def test_a_small_upright_jpeg_is_sent_as_uploaded(self):
        raw = _jpeg(800, 600)
        info, copy = d.plan_model_image(raw, "image/jpeg")
        assert copy is None
        assert (info.width, info.height, info.mime_type) == (800, 600, "image/jpeg")

    def test_a_large_photo_is_downscaled_under_the_byte_cap(self):
        raw = _jpeg(6000, 4000)
        assert len(raw) > d.MODEL_IMAGE_MAX_BYTES
        info, copy = d.plan_model_image(raw, "image/jpeg")
        assert copy is not None and copy.mime_type == "image/jpeg"
        assert (copy.width, copy.height) == d.target_size(6000, 4000)
        assert len(copy.data) <= d.MODEL_IMAGE_MAX_BYTES
        # Bedrock's limit is 5 MB base64-encoded.
        assert len(copy.data) * 4 / 3 < 5 * 1024 * 1024
        with Image.open(io.BytesIO(copy.data)) as out:
            assert out.size == (copy.width, copy.height)
            assert "exif" not in out.info

    def test_exif_orientation_is_applied(self):
        raw = _jpeg(1200, 800, orientation=6)
        info, copy = d.plan_model_image(raw, "image/jpeg")
        assert info.needs_transpose
        assert copy is not None and (copy.width, copy.height) == (800, 1200)

    def test_a_screenshot_stays_png(self):
        img = Image.new("RGB", (3200, 2000), (255, 255, 255))
        raw = _encode(img, "PNG")
        _, copy = d.plan_model_image(raw, "image/png")
        assert copy is not None and copy.mime_type == "image/png"

    def test_a_png_too_big_for_the_cap_becomes_jpeg_or_webp(self):
        opaque = _encode(_noise(2400, 1500), "PNG")
        _, copy = d.plan_model_image(opaque, "image/png")
        assert copy is not None and copy.mime_type == "image/jpeg"
        assert len(copy.data) <= d.MODEL_IMAGE_MAX_BYTES

        clear = _noise(1800, 1800, "RGBA")
        _, copy = d.plan_model_image(_encode(clear, "PNG"), "image/png")
        assert copy is not None and copy.mime_type == "image/webp"
        assert len(copy.data) <= d.MODEL_IMAGE_MAX_BYTES

    def test_a_mislabelled_image_gets_a_copy_in_its_real_format(self):
        raw = _jpeg(400, 300)
        _, copy = d.plan_model_image(raw, "image/png")
        assert copy is not None and copy.mime_type == "image/jpeg"

    def test_only_the_first_frame_of_an_animation(self):
        frames = [Image.new("RGB", (3000, 2000), c) for c in ((255, 0, 0), (0, 255, 0))]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        _, copy = d.plan_model_image(buf.getvalue(), "image/gif")
        assert copy is not None and copy.mime_type == "image/png"
        with Image.open(io.BytesIO(copy.data)) as out:
            assert getattr(out, "n_frames", 1) == 1
            assert out.convert("RGB").getpixel((10, 10)) == (255, 0, 0)

    def test_unreadable_bytes_raise(self):
        with pytest.raises(d.ImageUnreadableError):
            d.plan_model_image(b"not an image at all", "image/png")

    def test_a_decompression_bomb_is_refused(self, monkeypatch):
        monkeypatch.setattr(d, "MAX_SOURCE_PIXELS", 1_000_000)
        raw = _encode(Image.new("RGB", (3000, 3000)), "PNG")
        with pytest.raises(d.ImageUnreadableError):
            d.downscale(raw)


class TestDeterminism:
    """Phase 4a restores a turn's image from its stored copy: one input, one output."""

    def test_same_input_same_bytes_in_process(self):
        raw = _jpeg(5000, 3000)
        first = d.downscale(raw)
        second = d.downscale(raw)
        assert first.data == second.data

    def test_same_input_same_bytes_in_a_fresh_interpreter(self, tmp_path: Path):
        raw = _jpeg(4000, 3000, orientation=3)
        src = tmp_path / "in.jpg"
        src.write_bytes(raw)
        expected = hashlib.sha256(d.downscale(raw).data).hexdigest()
        script = textwrap.dedent(
            f"""
            import hashlib, pathlib
            from apis.shared.files.image_downscale import downscale
            print(hashlib.sha256(downscale(pathlib.Path({str(src)!r}).read_bytes()).data).hexdigest())
            """
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env)
        assert out.stdout.strip() == expected


def test_model_copy_lives_with_the_other_derived_sidecars():
    key = d.model_image_key("user-files/u/s/up-1/photo.heic.png", "image/jpeg")
    assert key == "user-files/u/s/up-1/_derived/model-image.jpg"
    compact = d.model_image_key("user-files/u/s/up-1/photo.png", "image/webp", d.COMPACT)
    assert compact == "user-files/u/s/up-1/_derived/model-image-compact.webp"


class TestCompactCopy:
    def test_standard_tier_and_small_enough_for_twenty(self):
        raw = _jpeg(6000, 4000)
        info, high, compact = d.plan_model_images(raw, "image/jpeg")
        assert high is not None and compact is not None
        assert max(compact.width, compact.height) <= d.COMPACT_LONG_EDGE_PX
        assert d.visual_tokens(compact.width, compact.height) <= d.COMPACT_VISUAL_TOKENS
        assert len(compact.data) <= d.COMPACT_IMAGE_MAX_BYTES
        assert 20 * d.COMPACT_IMAGE_MAX_BYTES < 10 * 1024 * 1024

    def test_worst_case_noise_png_with_alpha_still_fits(self):
        raw = _encode(_noise(2000, 2000, "RGBA"), "PNG")
        _, _, compact = d.plan_model_images(raw, "image/png")
        assert compact is not None and len(compact.data) <= d.COMPACT_IMAGE_MAX_BYTES

    def test_a_small_image_has_neither_copy(self):
        raw = _encode(Image.new("RGB", (640, 480), (1, 2, 3)), "PNG")
        _, high, compact = d.plan_model_images(raw, "image/png")
        assert high is None and compact is None

    def test_deterministic(self):
        raw = _jpeg(3000, 2000)
        assert d.downscale(raw, d.COMPACT).data == d.downscale(raw, d.COMPACT).data
