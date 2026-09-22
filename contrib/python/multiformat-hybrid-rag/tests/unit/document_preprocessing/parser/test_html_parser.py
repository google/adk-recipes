from unittest.mock import MagicMock, patch

import pytest

from src.document_preprocessing.parser.html_parser import (
    _clean_html,
    _has_content,
    parse,
    quick_raw_text,
)


class TestCleanHtml:
    def test_strips_script_tags(self):
        html = "<html><body><script>alert(1)</script><p>keep</p></body></html>"
        result = _clean_html(html)
        assert "alert" not in result
        assert "keep" in result

    def test_strips_style_tags(self):
        html = (
            "<html><body><style>.x{color:red}</style><p>keep</p></body></html>"
        )
        result = _clean_html(html)
        assert "color:red" not in result
        assert "keep" in result

    def test_strips_nav_tag(self):
        html = "<html><body><nav><a>Menu</a></nav><p>keep</p></body></html>"
        result = _clean_html(html)
        assert "Menu" not in result
        assert "keep" in result

    def test_strips_footer_tag(self):
        html = (
            "<html><body><footer>Footer text</footer><p>keep</p></body></html>"
        )
        result = _clean_html(html)
        assert "Footer text" not in result
        assert "keep" in result

    def test_strips_img_tags(self):
        html = '<html><body><img src="x.png"/><p>keep</p></body></html>'
        result = _clean_html(html)
        assert "img" not in result
        assert "keep" in result

    def test_strips_hidden_aria(self):
        html = '<html><body><div aria-hidden="true">hidden</div><p>keep</p></body></html>'
        result = _clean_html(html)
        assert "hidden" not in result.lower() or "keep" in result
        assert "keep" in result

    def test_strips_display_none(self):
        html = '<html><body><div style="display:none">hidden</div><p>keep</p></body></html>'
        result = _clean_html(html)
        assert "keep" in result

    def test_strips_hidden_attribute(self):
        html = "<html><body><div hidden>hidden</div><p>keep</p></body></html>"
        result = _clean_html(html)
        assert "keep" in result

    def test_strips_html_attributes(self):
        html = '<html><body><p class="big" id="p1">text</p></body></html>'
        result = _clean_html(html)
        assert "class=" not in result
        assert "id=" not in result
        assert "text" in result

    def test_removes_empty_tags(self):
        html = "<html><body><div></div><p>keep</p></body></html>"
        result = _clean_html(html)
        assert "keep" in result

    def test_keeps_content(self):
        html = "<html><body><h1>Title</h1><p>Paragraph text here.</p></body></html>"
        result = _clean_html(html)
        assert "Title" in result
        assert "Paragraph text here." in result


class TestHasContent:
    def test_enough_words_and_sentences(self):
        text = " ".join(["word"] * 60) + ". Second sentence. Third one."
        assert _has_content(text) is True

    def test_too_few_words(self):
        text = "Short. Text."
        assert _has_content(text) is False

    def test_no_sentences(self):
        text = " ".join(["word"] * 100)
        assert _has_content(text) is False


class TestParse:
    @patch("src.document_preprocessing.parser.html_parser.generate_gemini")
    @patch(
        "src.document_preprocessing.parser.html_parser.get_generate_content_config"
    )
    def test_successful_conversion(self, mock_config, mock_gemini, tmp_path):
        words = " ".join(["word"] * 60)
        html_content = f"<html><body><p>{words}. Second sentence. Third one.</p></body></html>"
        f = tmp_path / "page.html"
        f.write_text(html_content, encoding="utf-8")

        mock_config.return_value = MagicMock()
        mock_gemini.return_value = "# Converted markdown"

        client = MagicMock()
        result = parse(str(f), genai_client=client)

        assert result == "# Converted markdown"
        mock_gemini.assert_called_once()

    @patch("src.document_preprocessing.parser.html_parser.generate_gemini")
    @patch(
        "src.document_preprocessing.parser.html_parser.get_generate_content_config"
    )
    def test_insufficient_content_returns_empty(
        self, mock_config, mock_gemini, tmp_path
    ):
        html_content = "<html><body><p>short</p></body></html>"
        f = tmp_path / "sparse.html"
        f.write_text(html_content, encoding="utf-8")

        client = MagicMock()
        result = parse(str(f), genai_client=client)

        assert result == ""
        mock_gemini.assert_not_called()

    def test_missing_genai_client_raises(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>text</p></body></html>", encoding="utf-8")
        with pytest.raises(ValueError, match="genai_client is required"):
            parse(str(f))


class TestQuickRawText:
    def test_extracts_visible_text(self, tmp_path):
        html_content = (
            "<html><body>"
            "<script>var x=1;</script>"
            "<p>Visible text here</p>"
            "<nav>Menu</nav>"
            "</body></html>"
        )
        f = tmp_path / "page.html"
        f.write_text(html_content, encoding="utf-8")

        result = quick_raw_text(str(f))
        assert "Visible text here" in result
        assert "var x=1" not in result
        assert "Menu" not in result
