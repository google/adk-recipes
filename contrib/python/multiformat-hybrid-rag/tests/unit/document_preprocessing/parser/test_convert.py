from unittest.mock import MagicMock, patch

import pytest

from src.document_preprocessing.parser.convert import (
    _find_libreoffice,
    to_format,
    to_pdf,
)


class TestFindLibreOffice:
    @patch("src.document_preprocessing.parser.convert.shutil.which")
    def test_returns_libreoffice_path(self, mock_which):
        mock_which.side_effect = lambda name: (
            "/usr/bin/libreoffice" if name == "libreoffice" else None
        )
        assert _find_libreoffice() == "/usr/bin/libreoffice"

    @patch("src.document_preprocessing.parser.convert.shutil.which")
    def test_returns_soffice_path(self, mock_which):
        mock_which.side_effect = lambda name: (
            "/usr/bin/soffice" if name == "soffice" else None
        )
        assert _find_libreoffice() == "/usr/bin/soffice"

    @patch("src.document_preprocessing.parser.convert.shutil.which")
    def test_raises_when_not_found(self, mock_which):
        mock_which.return_value = None
        with pytest.raises(FileNotFoundError, match="LibreOffice not found"):
            _find_libreoffice()


class TestToPdf:
    @patch("src.document_preprocessing.parser.convert.subprocess.run")
    @patch("src.document_preprocessing.parser.convert._find_libreoffice")
    @patch("src.document_preprocessing.parser.convert.tempfile.mkdtemp")
    def test_calls_libreoffice_and_returns_path(
        self, mock_mkdtemp, mock_find, mock_run, tmp_path
    ):
        outdir = str(tmp_path)
        mock_mkdtemp.return_value = outdir
        mock_find.return_value = "/usr/bin/libreoffice"
        mock_run.return_value = MagicMock(stderr="")

        pdf_out = tmp_path / "doc.pdf"
        pdf_out.write_text("fake pdf")

        result = to_pdf("/some/path/doc.pptx")

        assert result == str(pdf_out)
        mock_run.assert_called_once()
        args = mock_run.call_args
        cmd = args[0][0]
        assert cmd[0] == "/usr/bin/libreoffice"
        assert "--headless" in cmd
        assert "--convert-to" in cmd
        assert "pdf" in cmd

    @patch("src.document_preprocessing.parser.convert.subprocess.run")
    @patch("src.document_preprocessing.parser.convert._find_libreoffice")
    @patch("src.document_preprocessing.parser.convert.tempfile.mkdtemp")
    def test_raises_on_failure(
        self, mock_mkdtemp, mock_find, mock_run, tmp_path
    ):
        mock_mkdtemp.return_value = str(tmp_path)
        mock_find.return_value = "/usr/bin/libreoffice"
        mock_run.return_value = MagicMock(stderr="conversion error")

        with pytest.raises(RuntimeError, match="LibreOffice conversion failed"):
            to_pdf("/some/path/doc.pptx")


class TestToFormat:
    @patch("src.document_preprocessing.parser.convert.subprocess.run")
    @patch("src.document_preprocessing.parser.convert._find_libreoffice")
    @patch("src.document_preprocessing.parser.convert.tempfile.mkdtemp")
    def test_calls_libreoffice_with_correct_format(
        self, mock_mkdtemp, mock_find, mock_run, tmp_path
    ):
        outdir = str(tmp_path)
        mock_mkdtemp.return_value = outdir
        mock_find.return_value = "/usr/bin/soffice"
        mock_run.return_value = MagicMock(stderr="")

        out_file = tmp_path / "doc.pptx"
        out_file.write_text("fake pptx")

        result = to_format("/some/path/doc.ppt", "pptx")

        assert result == str(out_file)
        cmd = mock_run.call_args[0][0]
        assert "pptx" in cmd
        assert "--headless" in cmd

    @patch("src.document_preprocessing.parser.convert.subprocess.run")
    @patch("src.document_preprocessing.parser.convert._find_libreoffice")
    @patch("src.document_preprocessing.parser.convert.tempfile.mkdtemp")
    def test_raises_on_failure(
        self, mock_mkdtemp, mock_find, mock_run, tmp_path
    ):
        mock_mkdtemp.return_value = str(tmp_path)
        mock_find.return_value = "/usr/bin/soffice"
        mock_run.return_value = MagicMock(stderr="error")

        with pytest.raises(RuntimeError, match="LibreOffice conversion failed"):
            to_format("/some/path/doc.ppt", "pptx")
