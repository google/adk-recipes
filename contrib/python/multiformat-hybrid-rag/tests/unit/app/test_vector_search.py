import logging

from app import vector_search
from app.vector_search import _extract_window


class TestExtractWindow:
    def test_returns_window_around_estimated_chunk_position(self):
        full_text = "".join(str(i % 10) for i in range(5000))

        window = _extract_window(
            full_text,
            chunk_index=2,
            chunk_size=1000,
            chunk_overlap=200,
            window_chars=100,
        )

        # estimated_start = 2 * 800 = 1600
        assert window == full_text[1500:2700]

    def test_clamps_start_at_zero_for_first_chunk(self):
        full_text = "abcdefghij" * 100

        window = _extract_window(
            full_text,
            chunk_index=0,
            chunk_size=200,
            chunk_overlap=50,
            window_chars=100,
        )

        assert window == full_text[0:300]

    def test_clamps_end_at_document_length(self):
        full_text = "abcdefghij" * 10

        window = _extract_window(
            full_text,
            chunk_index=0,
            chunk_size=1000,
            chunk_overlap=200,
            window_chars=500,
        )

        assert window == full_text

    def test_chunk_index_past_end_returns_tail_not_empty_string(self):
        """A chunk_id can outrun its stored document when the document was
        re-ingested shorter than the indexed version. Unclamped, start
        exceeded end and the slice silently returned "".
        """
        full_text = "A" * 500

        window = _extract_window(
            full_text,
            chunk_index=50,
            chunk_size=1000,
            chunk_overlap=200,
            window_chars=100,
        )

        assert window != ""
        assert window == full_text[400:500]

    def test_window_is_never_empty_for_non_empty_document(self):
        full_text = "x" * 300

        for chunk_index in range(0, 200, 7):
            window = _extract_window(
                full_text,
                chunk_index=chunk_index,
                chunk_size=500,
                chunk_overlap=100,
                window_chars=50,
            )
            assert window != "", f"empty window at chunk_index={chunk_index}"


class TestChunkIdContract:
    """chunk_id format is a contract between three separately deployed
    pieces, so the separator must come from one shared definition.
    """

    def test_minted_id_parses_back_to_its_index(self):
        from src.utils import CHUNK_ID_SEPARATOR

        file_id, chunk_index = "abc123", 7
        chunk_id = f"{file_id}{CHUNK_ID_SEPARATOR}{chunk_index}"

        parsed = int(chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[1])

        assert parsed == chunk_index

    def test_file_ids_containing_the_separator_still_parse(self):
        from src.utils import CHUNK_ID_SEPARATOR

        # rsplit, not split: only the final segment is the index.
        file_id = f"weird{CHUNK_ID_SEPARATOR}name"
        chunk_id = f"{file_id}{CHUNK_ID_SEPARATOR}12"

        assert int(chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[1]) == 12
        assert chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[0] == file_id


class TestIntegrationStubSeam:
    """The stub exists because the e2e test starts the app as a subprocess.
    It must never be reachable from a deployed service.
    """

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("INTEGRATION_TEST", raising=False)
        assert vector_search._stub_enabled() is False

    def test_enabled_for_the_integration_test(self, monkeypatch):
        monkeypatch.setenv("INTEGRATION_TEST", "TRUE")
        assert vector_search._stub_enabled() is True

    def test_warns_loudly_whenever_it_fires(self, monkeypatch, caplog):
        monkeypatch.setenv("INTEGRATION_TEST", "TRUE")
        with caplog.at_level(logging.WARNING):
            vector_search._stub_enabled()
        assert "Vector Search is NOT being queried" in caplog.text

    def test_any_other_value_does_not_enable_it(self, monkeypatch):
        for value in ("true", "1", "yes", ""):
            monkeypatch.setenv("INTEGRATION_TEST", value)
            assert vector_search._stub_enabled() is False
