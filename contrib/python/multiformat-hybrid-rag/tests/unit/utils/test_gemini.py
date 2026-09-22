from unittest.mock import MagicMock

import pytest
from google.genai import types

from src.utils.gemini import generate_gemini
from src.utils.llm_utils import get_generate_content_config


class TestGenerateGemini:
    def test_successful_generation_returns_text(self):
        mock_client = MagicMock()
        mock_part = MagicMock()
        mock_part.text = "  generated answer  "
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_result = MagicMock()
        mock_result.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_result

        result = generate_gemini(["What is 2+2?"], client=mock_client)

        assert result == "generated answer"
        mock_client.models.generate_content.assert_called_once()

    def test_empty_candidates_raises_value_error(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.candidates = []
        mock_result.prompt_feedback.block_reason = "SAFETY"
        mock_client.models.generate_content.return_value = mock_result

        with pytest.raises(ValueError, match="no candidates"):
            generate_gemini(["bad prompt"], client=mock_client)

    def test_empty_parts_returns_empty_string(self):
        mock_client = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.content.parts = []
        mock_result = MagicMock()
        mock_result.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_result

        result = generate_gemini(["prompt"], client=mock_client)
        assert result == ""

    def test_uses_provided_config(self):
        mock_client = MagicMock()
        mock_part = MagicMock()
        mock_part.text = "answer"
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_result = MagicMock()
        mock_result.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_result

        custom_config = get_generate_content_config(temperature=0.5)
        generate_gemini(["q"], client=mock_client, config=custom_config)

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["config"] is custom_config

    def test_uses_default_config_when_none(self):
        mock_client = MagicMock()
        mock_part = MagicMock()
        mock_part.text = "answer"
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_result = MagicMock()
        mock_result.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_result

        generate_gemini(["q"], client=mock_client, config=None)

        call_kwargs = mock_client.models.generate_content.call_args
        used_config = call_kwargs.kwargs["config"]
        assert isinstance(used_config, types.GenerateContentConfig)
        assert used_config.temperature == 0
