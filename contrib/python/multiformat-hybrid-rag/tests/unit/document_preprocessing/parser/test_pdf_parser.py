from unittest.mock import MagicMock, patch

import pytest

from src.document_preprocessing.parser.pdf_parser import (
    _split_pdf_pages,
    parse,
    quick_raw_text,
)


class TestQuickRawText:
    @patch("src.document_preprocessing.parser.pdf_parser.pymupdf")
    def test_returns_text_from_pages(self, mock_pymupdf):
        page1 = MagicMock()
        page1.get_text.return_value = "Page one text"
        page2 = MagicMock()
        page2.get_text.return_value = "Page two text"

        mock_doc = MagicMock()
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
        mock_pymupdf.open.return_value = mock_doc

        result = quick_raw_text("/tmp/test.pdf")

        assert "Page one text" in result
        assert "Page two text" in result
        mock_pymupdf.open.assert_called_once_with("/tmp/test.pdf")


class TestSplitPdfPages:
    @patch("src.document_preprocessing.parser.pdf_parser.pymupdf")
    def test_returns_page_bytes_and_text(self, mock_pymupdf):
        page0 = MagicMock()
        page0.get_text.return_value = "text page 0"

        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.load_page.return_value = page0
        mock_pymupdf.open.side_effect = [mock_doc, MagicMock()]

        single_doc = mock_pymupdf.open.return_value
        single_doc.tobytes.return_value = b"single page bytes"

        pages, texts = _split_pdf_pages("/tmp/test.pdf")

        assert len(texts) == 1
        assert texts[0] == "text page 0"
        assert len(pages) == 1
        mock_doc.close.assert_called_once()


class TestParse:
    @patch("src.document_preprocessing.parser.pdf_parser.parse_pdf_pages")
    @patch("src.document_preprocessing.parser.pdf_parser._split_pdf_pages")
    def test_splits_and_calls_parse_pdf_pages(
        self, mock_split, mock_parse_pages
    ):
        mock_split.return_value = ([b"p1", b"p2"], ["t1", "t2"])
        mock_parse_pages.return_value = "# Page 1\n\n---\n\n# Page 2"

        client = MagicMock()
        result = parse("/tmp/test.pdf", genai_client=client, max_workers=4)

        assert result == "# Page 1\n\n---\n\n# Page 2"
        mock_split.assert_called_once_with("/tmp/test.pdf")
        mock_parse_pages.assert_called_once_with(
            [b"p1", b"p2"], client, texts=["t1", "t2"], max_workers=4
        )

    def test_raises_without_genai_client(self):
        with pytest.raises(ValueError, match="genai_client is required"):
            parse("/tmp/test.pdf")

    @patch("src.document_preprocessing.parser.pdf_parser.parse_pdf_pages")
    @patch("src.document_preprocessing.parser.pdf_parser._split_pdf_pages")
    def test_joins_results_with_separator(self, mock_split, mock_parse_pages):
        mock_split.return_value = ([b"p1"], ["t1"])
        mock_parse_pages.return_value = "single page"

        result = parse("/tmp/test.pdf", genai_client=MagicMock())
        assert result == "single page"
