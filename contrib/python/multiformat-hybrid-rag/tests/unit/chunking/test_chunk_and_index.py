from unittest.mock import MagicMock

from src.chunking.chunk_and_index import (
    _append_chunks_batch,
    _build_rechunk_query,
    _prepare_chunks_staging,
    _staging_table_name,
)


class TestStagingTableName:
    def test_correct_format(self):
        result = _staging_table_name("proj.dataset.chunks", "12345_abcd")
        assert result == "proj.dataset._staging_chunk_rows_12345_abcd"

    def test_preserves_project_and_dataset(self):
        result = _staging_table_name("my-proj.my_ds.my_chunks", "run99")
        assert result == "my-proj.my_ds._staging_chunk_rows_run99"

    def test_different_run_ids_give_different_names(self):
        a = _staging_table_name("p.d.t", "run_a")
        b = _staging_table_name("p.d.t", "run_b")
        assert a != b


class TestPrepareChunksStaging:
    def test_calls_bq_load_with_truncate_disposition(self):
        bq = MagicMock()
        load_job = MagicMock()
        bq.load_table_from_json.return_value = load_job

        _prepare_chunks_staging(bq, "proj.ds.chunks", "run1")

        bq.load_table_from_json.assert_called_once()
        args, kwargs = bq.load_table_from_json.call_args
        assert args[0] == []
        assert "staging_chunk_rows_run1" in args[1]
        job_config = kwargs["job_config"]
        assert job_config.write_disposition == "WRITE_TRUNCATE"
        load_job.result.assert_called_once()

    def test_schema_has_expected_fields(self):
        bq = MagicMock()
        bq.load_table_from_json.return_value = MagicMock()

        _prepare_chunks_staging(bq, "proj.ds.chunks", "run1")

        job_config = bq.load_table_from_json.call_args.kwargs["job_config"]
        field_names = {f.name for f in job_config.schema}
        assert field_names == {
            "chunk_id",
            "file_id",
            "gcs_uri",
            "chunk_index",
            "chunk_text",
            "context",
        }


class TestAppendChunksBatch:
    def test_calls_bq_load_with_append_disposition(self):
        bq = MagicMock()
        bq.load_table_from_json.return_value = MagicMock()

        rows = [{"chunk_id": "c1", "file_id": "f1"}]
        _append_chunks_batch(bq, "proj.ds.chunks", rows, "run1")

        bq.load_table_from_json.assert_called_once()
        args, kwargs = bq.load_table_from_json.call_args
        assert args[0] == rows
        job_config = kwargs["job_config"]
        assert job_config.write_disposition == "WRITE_APPEND"

    def test_noop_on_empty_rows(self):
        bq = MagicMock()
        _append_chunks_batch(bq, "proj.ds.chunks", [], "run1")
        bq.load_table_from_json.assert_not_called()


class TestFlushChunksStaging:
    def test_executes_insert_then_drops_staging(self):
        from src.chunking.chunk_and_index import _flush_chunks_staging

        bq = MagicMock()
        query_job = MagicMock()
        bq.query.return_value = query_job

        _flush_chunks_staging(bq, "proj.ds.chunks", "run1")

        bq.query.assert_called_once()
        sql = bq.query.call_args[0][0]
        assert "INSERT INTO" in sql
        assert "proj.ds.chunks" in sql
        assert "staging_chunk_rows_run1" in sql
        assert "CURRENT_TIMESTAMP()" in sql
        query_job.result.assert_called_once()

        bq.delete_table.assert_called_once()
        delete_args = bq.delete_table.call_args
        assert "staging_chunk_rows_run1" in delete_args[0][0]
        assert delete_args[1]["not_found_ok"] is True


class TestBuildRechunkQuery:
    def test_incremental_mode_contains_candidates_cte(self):
        sql = _build_rechunk_query(
            "proj.ds.prep", "proj.ds.chunks", rechunk_all=False
        )
        assert "candidates" in sql
        assert "last_indexed_per_file" in sql
        assert "proj.ds.prep" in sql
        assert "proj.ds.chunks" in sql

    def test_rechunk_all_mode_selects_all_relevant(self):
        sql = _build_rechunk_query(
            "proj.ds.prep", "proj.ds.chunks", rechunk_all=True
        )
        assert "relevant_files" in sql
        assert "relevant IS NOT FALSE" in sql
        assert "proj.ds.prep" in sql

    def test_rechunk_all_does_not_reference_chunks_table(self):
        sql = _build_rechunk_query(
            "proj.ds.prep", "proj.ds.chunks", rechunk_all=True
        )
        assert "proj.ds.chunks" not in sql

    def test_incremental_references_both_tables(self):
        sql = _build_rechunk_query(
            "proj.ds.prep", "proj.ds.chunks", rechunk_all=False
        )
        assert "proj.ds.prep" in sql
        assert "proj.ds.chunks" in sql

    def test_incremental_uses_extracted_at_comparison(self):
        sql = _build_rechunk_query(
            "proj.ds.prep", "proj.ds.chunks", rechunk_all=False
        )
        assert "extracted_at" in sql
        assert "last_indexed" in sql
