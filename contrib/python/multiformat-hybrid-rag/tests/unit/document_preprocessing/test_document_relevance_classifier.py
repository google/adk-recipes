from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_pipeline_config():
    cfg = MagicMock()
    cfg.relevance_gemini_model = "gemini-test-model"
    with patch(
        "src.document_preprocessing.document_relevance_classifier.pipeline_config",
        cfg,
    ):
        yield cfg


@pytest.fixture()
def mock_generate_gemini():
    with patch(
        "src.document_preprocessing.document_relevance_classifier.generate_gemini",
    ) as m:
        yield m


@pytest.fixture()
def _classifier_deps(mock_pipeline_config, mock_generate_gemini):
    pass


@pytest.fixture()
def classify(mock_pipeline_config, mock_generate_gemini):
    from src.document_preprocessing.document_relevance_classifier import (
        classify_relevance,
    )

    return classify_relevance


class TestClassifyRelevance:
    def test_short_content_returns_false_without_calling_gemini(
        self, classify, mock_generate_gemini
    ):
        result = classify("short", MagicMock())
        assert result is False
        mock_generate_gemini.assert_not_called()

    def test_short_content_threshold_49_chars(
        self, classify, mock_generate_gemini
    ):
        result = classify("a" * 49, MagicMock())
        assert result is False
        mock_generate_gemini.assert_not_called()

    def test_returns_true_when_gemini_says_relevant(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "RELEVANT"
        result = classify("a" * 100, MagicMock())
        assert result is True

    def test_returns_false_when_gemini_says_irrelevant(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "IRRELEVANT"
        result = classify("a" * 100, MagicMock())
        assert result is False

    def test_defaults_to_relevant_on_unexpected_output(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "MAYBE"
        result = classify("a" * 100, MagicMock())
        assert result is True

    def test_defaults_to_relevant_on_exception(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.side_effect = RuntimeError("API down")
        result = classify("a" * 100, MagicMock())
        assert result is True

    def test_case_insensitive_relevant_lowercase(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "relevant"
        result = classify("a" * 100, MagicMock())
        assert result is True

    def test_case_insensitive_relevant_mixed(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "Relevant"
        result = classify("a" * 100, MagicMock())
        assert result is True

    def test_case_insensitive_irrelevant_lowercase(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "irrelevant"
        result = classify("a" * 100, MagicMock())
        assert result is False

    def test_exactly_50_chars_calls_gemini(
        self, classify, mock_generate_gemini
    ):
        mock_generate_gemini.return_value = "RELEVANT"
        result = classify("a" * 50, MagicMock())
        assert result is True
        mock_generate_gemini.assert_called_once()

    def test_genai_client_forwarded(
        self, classify, mock_generate_gemini, mock_pipeline_config
    ):
        mock_generate_gemini.return_value = "RELEVANT"
        client = MagicMock()
        classify("a" * 100, client)
        call_kwargs = mock_generate_gemini.call_args
        assert call_kwargs.kwargs["client"] is client
        assert call_kwargs.kwargs["model"] == "gemini-test-model"
