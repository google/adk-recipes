from unittest.mock import MagicMock, patch

import pytest

from src.document_preprocessing.parser import parse, quick_raw_text


class TestParse:
    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_routes_pdf(self, mock_mime, mock_map):
        mock_mime.return_value = "application/pdf"
        mock_parser = MagicMock()
        mock_parser.parse.return_value = "# PDF content"
        mock_map.get.return_value = mock_parser

        result = parse("/tmp/test.pdf", genai_client=MagicMock())
        assert result == "# PDF content"
        mock_parser.parse.assert_called_once()

    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_routes_json(self, mock_mime, mock_map):
        mock_mime.return_value = "application/json"
        mock_parser = MagicMock()
        mock_parser.parse.return_value = "**key**: value"
        mock_map.get.return_value = mock_parser

        result = parse("/tmp/test.json")
        assert result == "**key**: value"
        mock_parser.parse.assert_called_once()

    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_routes_html(self, mock_mime, mock_map):
        mock_mime.return_value = "text/html"
        mock_parser = MagicMock()
        mock_parser.parse.return_value = "# HTML content"
        mock_map.get.return_value = mock_parser

        result = parse("/tmp/test.html", genai_client=MagicMock())
        assert result == "# HTML content"
        mock_parser.parse.assert_called_once()

    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_routes_xlsx(self, mock_mime, mock_map):
        mock_mime.return_value = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        mock_parser = MagicMock()
        mock_parser.parse.return_value = "| A | B |"
        mock_map.get.return_value = mock_parser

        result = parse("/tmp/test.xlsx")
        assert result == "| A | B |"

    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_markdown_reads_directly(self, mock_mime, tmp_path):
        mock_mime.return_value = "text/markdown"
        f = tmp_path / "doc.md"
        f.write_text("# Hello", encoding="utf-8")

        result = parse(str(f))
        assert result == "# Hello"

    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_txt_reads_directly(self, mock_mime, tmp_path):
        mock_mime.return_value = "text/plain"
        f = tmp_path / "doc.txt"
        f.write_text("plain text", encoding="utf-8")

        result = parse(str(f))
        assert result == "plain text"

    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_unsupported_format_raises(self, mock_mime):
        mock_mime.return_value = "application/octet-stream"
        with pytest.raises(ValueError, match="Unsupported format"):
            parse("/tmp/test.bin")


class TestQuickRawText:
    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_delegates_to_parser(self, mock_mime, mock_map):
        mock_mime.return_value = "application/pdf"
        mock_parser = MagicMock()
        mock_parser.quick_raw_text.return_value = "raw text from pdf"
        mock_map.get.return_value = mock_parser

        result = quick_raw_text("/tmp/test.pdf")
        assert result == "raw text from pdf"
        mock_parser.quick_raw_text.assert_called_once_with("/tmp/test.pdf")

    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_returns_none_if_no_quick_raw_text(self, mock_mime, mock_map):
        mock_mime.return_value = "text/html"
        mock_parser = MagicMock(spec=[])
        mock_map.get.return_value = mock_parser

        result = quick_raw_text("/tmp/test.html")
        assert result is None

    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_returns_none_if_no_parser(self, mock_mime):
        mock_mime.return_value = "application/octet-stream"
        result = quick_raw_text("/tmp/test.bin")
        assert result is None

    @patch("src.document_preprocessing.parser._PARSER_MAP")
    @patch("src.document_preprocessing.parser.get_mime_type_from_path")
    def test_returns_none_on_exception(self, mock_mime, mock_map):
        mock_mime.return_value = "application/pdf"
        mock_parser = MagicMock()
        mock_parser.quick_raw_text.side_effect = RuntimeError("fail")
        mock_map.get.return_value = mock_parser

        result = quick_raw_text("/tmp/test.pdf")
        assert result is None
