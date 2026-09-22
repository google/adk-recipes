from src.utils.llm_utils import (
    get_generate_content_config,
    get_mime_type_from_bytes,
    get_mime_type_from_path,
    get_part,
)


class TestGetMimeTypeFromBytes:
    def test_png(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "image/png"

    def test_jpeg(self):
        data = b"\xff\xd8" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "image/jpeg"

    def test_webp(self):
        data = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "image/webp"

    def test_gif(self):
        data = b"GIF89a" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "image/gif"

    def test_pdf(self):
        data = b"%PDF-1.4" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "application/pdf"

    def test_mp4(self):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "video/mp4"

    def test_webm(self):
        data = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
        assert get_mime_type_from_bytes(data) == "video/webm"

    def test_unknown(self):
        data = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d"
        assert get_mime_type_from_bytes(data) == "application/octet-stream"

    def test_short_data(self):
        data = b"\x89PNG"
        assert get_mime_type_from_bytes(data) == "application/octet-stream"


class TestGetMimeTypeFromPath:
    def test_pdf(self):
        assert get_mime_type_from_path("doc.pdf") == "application/pdf"

    def test_docx(self):
        assert get_mime_type_from_path("report.docx") == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_html(self):
        assert get_mime_type_from_path("page.html") == "text/html"

    def test_json(self):
        assert get_mime_type_from_path("data.json") == "application/json"

    def test_jsonl(self):
        assert get_mime_type_from_path("data.jsonl") == "application/jsonl"

    def test_unknown_extension(self):
        assert (
            get_mime_type_from_path("file.unknown")
            == "application/octet-stream"
        )


class TestGetGenerateContentConfig:
    def test_default_params(self):
        config = get_generate_content_config()
        assert config.temperature == 1
        assert config.top_p == 0.95
        assert config.max_output_tokens == 32768
        assert config.safety_settings is not None
        assert len(config.safety_settings) == 4

    def test_with_system_instruction(self):
        config = get_generate_content_config(system_instruction="Be helpful.")
        assert config.system_instruction is not None
        assert len(config.system_instruction) == 1

    def test_with_thinking_budget(self):
        config = get_generate_content_config(thinking_budget=1024)
        assert config.thinking_config is not None
        assert config.thinking_config.thinking_budget == 1024

    def test_with_thinking_level(self):
        config = get_generate_content_config(thinking_level="LOW")
        assert config.thinking_config is not None
        assert config.thinking_config.thinking_level == "LOW"

    def test_thinking_level_takes_precedence_over_budget(self):
        config = get_generate_content_config(
            thinking_level="HIGH", thinking_budget=512
        )
        assert config.thinking_config.thinking_level == "HIGH"
        assert config.thinking_config.thinking_budget is None

    def test_safety_off_false(self):
        config = get_generate_content_config(safety_off=False)
        assert config.safety_settings is None

    def test_with_response_schema(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        config = get_generate_content_config(response_schema=schema)
        assert config.response_schema == schema


class TestGetPart:
    def test_text_input(self):
        part = get_part("hello world")
        assert part.text == "hello world"

    def test_bytes_input(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        part = get_part(data)
        assert part.inline_data is not None
        assert part.inline_data.mime_type == "image/png"

    def test_gcs_uri_input(self):
        uri = "gs://bucket/path/to/file.pdf"
        part = get_part(uri)
        assert part.file_data is not None
        assert part.file_data.file_uri == uri
        assert part.file_data.mime_type == "application/pdf"
