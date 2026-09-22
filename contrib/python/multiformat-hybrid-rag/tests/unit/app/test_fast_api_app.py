from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

# Importing app.fast_api_app builds the ADK app at module scope, and
# get_fast_api_app(otel_to_cloud=True) calls google.auth.default() while
# doing so. That is ADK's behaviour, not the recipe's, but it means this
# module cannot be imported without credentials -- which CI does not have.
# Patch ADC for the duration of the import only.
with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
    from app.config import MAX_TOP_K
    from app.fast_api_app import SearchRequest


class TestSearchRequestValidation:
    """/api/search is a public trust boundary: top_k fans out into a Vector
    Search request per retriever and then into the Gemini prompt.
    """

    def test_defaults(self):
        req = SearchRequest(query="q")
        assert req.top_k == 10
        assert req.generative_answer is True

    def test_accepts_the_upper_bound(self):
        assert SearchRequest(query="q", top_k=MAX_TOP_K).top_k == MAX_TOP_K

    @pytest.mark.parametrize("bad", [MAX_TOP_K + 1, 10**9])
    def test_rejects_values_above_the_bound(self, bad):
        with pytest.raises(ValidationError):
            SearchRequest(query="q", top_k=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_non_positive(self, bad):
        with pytest.raises(ValidationError):
            SearchRequest(query="q", top_k=bad)
