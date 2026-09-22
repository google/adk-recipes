from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture()
def mock_config():
    cfg = MagicMock()
    cfg.gcs_bucket = "my-test-bucket"
    with patch("src.removal.propagate_gcs_deletions.config", cfg):
        yield cfg


@pytest.fixture()
def mock_bq_utils():
    with patch("src.removal.propagate_gcs_deletions.bq_utils") as m:
        yield m


@pytest.fixture()
def mock_vs_utils():
    with patch("src.removal.propagate_gcs_deletions.vs_utils") as m:
        yield m


class TestFindDeletedFiles:
    def test_delegates_to_bq_utils(self, mock_bq_utils):
        from src.removal.propagate_gcs_deletions import find_deleted_files

        bq = MagicMock()
        expected = [{"file_id": "f1", "gcs_uri": "gs://b/f.pdf"}]
        mock_bq_utils.get_deleted_files.return_value = expected

        result = find_deleted_files(bq, "proj.ds.obj", "proj.ds.prep")

        mock_bq_utils.get_deleted_files.assert_called_once_with(
            bq, "proj.ds.obj", "proj.ds.prep"
        )
        assert result == expected


class TestRun:
    def test_returns_empty_list_when_no_deleted_files(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        mock_bq_utils.get_deleted_files.return_value = []
        bq = MagicMock()

        result = run(
            bq,
            "col/path",
            "doc/col/path",
            "proj.ds.obj",
            "proj.ds.prep",
            "proj.ds.chunks",
        )

        assert result == []
        mock_vs_utils.delete_data_objects_by_file_ids.assert_not_called()
        mock_vs_utils.delete_documents_by_file_ids.assert_not_called()
        mock_bq_utils.delete_by_file_ids.assert_not_called()

    def test_cascade_delete_in_correct_order(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        deleted_files = [
            {"file_id": "f1", "gcs_uri": "gs://my-test-bucket/docs/a.pdf"},
            {"file_id": "f2", "gcs_uri": "gs://my-test-bucket/docs/b.pdf"},
        ]
        mock_bq_utils.get_deleted_files.return_value = deleted_files
        mock_vs_utils.delete_data_objects_by_file_ids.return_value = 5
        mock_vs_utils.delete_documents_by_file_ids.return_value = 2
        mock_bq_utils.delete_by_file_ids.return_value = 3
        bq = MagicMock()

        result = run(
            bq,
            "col/path",
            "doc/col/path",
            "proj.ds.obj",
            "proj.ds.prep",
            "proj.ds.chunks",
        )

        assert set(result) == {"f1", "f2"}

        mock_vs_utils.delete_data_objects_by_file_ids.assert_called_once_with(
            "col/path", ["f1", "f2"]
        )
        mock_vs_utils.delete_documents_by_file_ids.assert_called_once_with(
            "doc/col/path", ["f1", "f2"]
        )

        bq_delete_calls = mock_bq_utils.delete_by_file_ids.call_args_list
        assert len(bq_delete_calls) == 2
        assert bq_delete_calls[0] == call(bq, "proj.ds.chunks", ["f1", "f2"])
        assert bq_delete_calls[1] == call(bq, "proj.ds.prep", ["f1", "f2"])

    def test_vs2_chunks_deleted_before_vs2_docs(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        call_order = []
        mock_bq_utils.get_deleted_files.return_value = [
            {"file_id": "f1", "gcs_uri": "gs://my-test-bucket/f.pdf"},
        ]
        mock_vs_utils.delete_data_objects_by_file_ids.side_effect = lambda *a: (
            call_order.append("vs2_chunks") or 1
        )
        mock_vs_utils.delete_documents_by_file_ids.side_effect = lambda *a: (
            call_order.append("vs2_docs") or 1
        )
        mock_bq_utils.delete_by_file_ids.side_effect = lambda *a: (
            call_order.append("bq_delete") or 1
        )
        bq = MagicMock()

        run(bq, "c", "d", "o", "p", "ch")

        assert call_order == [
            "vs2_chunks",
            "vs2_docs",
            "bq_delete",
            "bq_delete",
        ]

    def test_safety_guard_raises_on_bucket_mismatch(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        mock_bq_utils.get_deleted_files.return_value = [
            {"file_id": "f1", "gcs_uri": "gs://wrong-bucket/docs/a.pdf"},
        ]
        bq = MagicMock()

        with pytest.raises(RuntimeError, match="safety guard"):
            run(bq, "c", "d", "o", "p", "ch")

        mock_vs_utils.delete_data_objects_by_file_ids.assert_not_called()
        mock_bq_utils.delete_by_file_ids.assert_not_called()

    def test_safety_guard_partial_mismatch_raises(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        mock_bq_utils.get_deleted_files.return_value = [
            {"file_id": "f1", "gcs_uri": "gs://my-test-bucket/ok.pdf"},
            {"file_id": "f2", "gcs_uri": "gs://other-bucket/bad.pdf"},
        ]
        bq = MagicMock()

        with pytest.raises(RuntimeError, match="safety guard"):
            run(bq, "c", "d", "o", "p", "ch")

    def test_returns_file_ids_on_success(
        self, mock_config, mock_bq_utils, mock_vs_utils
    ):
        from src.removal.propagate_gcs_deletions import run

        mock_bq_utils.get_deleted_files.return_value = [
            {"file_id": "abc", "gcs_uri": "gs://my-test-bucket/x.pdf"},
            {"file_id": "def", "gcs_uri": "gs://my-test-bucket/y.pdf"},
        ]
        mock_vs_utils.delete_data_objects_by_file_ids.return_value = 0
        mock_vs_utils.delete_documents_by_file_ids.return_value = 0
        mock_bq_utils.delete_by_file_ids.return_value = 0
        bq = MagicMock()

        result = run(bq, "c", "d", "o", "p", "ch")

        assert result == ["abc", "def"]
