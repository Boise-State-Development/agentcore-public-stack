"""
Tests for ImageHandler — image format detection and ContentBlock creation.

Validates: Requirements 8.1–8.7
"""

from agents.main_agent.multimodal.image_handler import ImageHandler


class TestIsImage:
    """Tests for ImageHandler.is_image"""

    # Req 8.1: content_type starts with "image/" → True
    def test_is_image_by_content_type_png(self):
        assert ImageHandler.is_image("image/png", "file.txt") is True

    def test_is_image_by_content_type_jpeg(self):
        assert ImageHandler.is_image("image/jpeg", "file.txt") is True

    def test_is_image_by_content_type_gif(self):
        assert ImageHandler.is_image("image/gif", "noext") is True

    def test_is_image_by_content_type_webp(self):
        assert ImageHandler.is_image("image/webp", "noext") is True

    def test_is_image_by_content_type_case_insensitive(self):
        assert ImageHandler.is_image("Image/PNG", "noext") is True

    # Req 8.2: filename ends with image extension → True
    def test_is_image_by_extension_png(self):
        assert ImageHandler.is_image("application/octet-stream", "photo.png") is True

    def test_is_image_by_extension_jpg(self):
        assert ImageHandler.is_image("application/octet-stream", "photo.jpg") is True

    def test_is_image_by_extension_jpeg(self):
        assert ImageHandler.is_image("application/octet-stream", "photo.jpeg") is True

    def test_is_image_by_extension_gif(self):
        assert ImageHandler.is_image("application/octet-stream", "anim.gif") is True

    def test_is_image_by_extension_webp(self):
        assert ImageHandler.is_image("application/octet-stream", "pic.webp") is True

    def test_is_image_by_extension_case_insensitive(self):
        assert ImageHandler.is_image("application/octet-stream", "PHOTO.JPG") is True

    # Req 8.3: not image content_type and no image extension → False
    def test_is_not_image_text_file(self):
        assert ImageHandler.is_image("text/plain", "readme.txt") is False

    def test_is_not_image_pdf(self):
        assert ImageHandler.is_image("application/pdf", "doc.pdf") is False

    def test_is_not_image_no_extension(self):
        assert ImageHandler.is_image("application/octet-stream", "data") is False


class TestGetImageFormat:
    """Tests for ImageHandler.get_image_format"""

    # Req 8.4: returns "png" for image/png and .png
    def test_format_png_by_content_type(self):
        assert ImageHandler.get_image_format("image/png", "file.txt") == "png"

    def test_format_png_by_extension(self):
        assert ImageHandler.get_image_format("application/octet-stream", "photo.png") == "png"

    # Req 8.5: returns "jpeg" for image/jpeg, image/jpg, .jpg, .jpeg
    def test_format_jpeg_by_content_type_jpeg(self):
        assert ImageHandler.get_image_format("image/jpeg", "file.txt") == "jpeg"

    def test_format_jpeg_by_content_type_jpg(self):
        assert ImageHandler.get_image_format("image/jpg", "file.txt") == "jpeg"

    def test_format_jpeg_by_extension_jpg(self):
        assert ImageHandler.get_image_format("application/octet-stream", "photo.jpg") == "jpeg"

    def test_format_jpeg_by_extension_jpeg(self):
        assert ImageHandler.get_image_format("application/octet-stream", "photo.jpeg") == "jpeg"

    # Req 8.6: defaults to "png" for unrecognized formats
    def test_format_default_png(self):
        assert ImageHandler.get_image_format("application/octet-stream", "file.bin") == "png"

    # Additional format coverage
    def test_format_gif(self):
        assert ImageHandler.get_image_format("image/gif", "anim.gif") == "gif"

    def test_format_webp(self):
        assert ImageHandler.get_image_format("image/webp", "pic.webp") == "webp"


class TestCreateContentBlock:
    """Tests for ImageHandler.create_content_block"""

    # Req 8.7: returns dict with "image" key containing "format" and "source.bytes"
    def test_content_block_structure(self):
        data = b"\x89PNG\r\n\x1a\n"
        block = ImageHandler.create_content_block(data, "image/png", "test.png")

        assert "image" in block
        assert block["image"]["format"] == "png"
        assert "source" in block["image"]
        assert block["image"]["source"]["bytes"] == data

    def test_content_block_jpeg(self):
        data = b"\xff\xd8\xff\xe0"
        block = ImageHandler.create_content_block(data, "image/jpeg", "photo.jpg")

        assert block["image"]["format"] == "jpeg"
        assert block["image"]["source"]["bytes"] == data

    def test_content_block_preserves_bytes(self):
        data = b"arbitrary bytes content"
        block = ImageHandler.create_content_block(data, "image/webp", "pic.webp")

        assert block["image"]["format"] == "webp"
        assert block["image"]["source"]["bytes"] is data

    def test_content_type_wins_over_the_filename(self):
        """A stored model copy can be another format than its name (a PNG sent as WebP)."""
        assert ImageHandler.get_image_format("image/webp", "shot.png") == "webp"
        assert ImageHandler.get_image_format("image/jpeg", "scan.png") == "jpeg"
        assert ImageHandler.get_image_format("", "shot.png") == "png"
