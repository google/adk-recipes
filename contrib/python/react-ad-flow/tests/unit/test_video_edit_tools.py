"""Unit tests for app.tools.video_edit_tools — fully network-free: the
Omni Flash interactions client is always mocked. Live behavior against the
real API is covered separately by tests/live/test_omni_video_edit.py."""

from unittest.mock import MagicMock, patch

import pytest

from app.tools import video_edit_tools


def _completed_interaction(video_b64):
    """A minimal stand-in for a completed google.genai Interaction object."""
    content = MagicMock()
    content.type = "video"
    content.data = video_b64
    content.uri = None
    step = MagicMock()
    step.type = "model_output"
    step.content = [content]
    interaction = MagicMock()
    interaction.status = "completed"
    interaction.steps = [step]
    interaction.id = "interaction-123"
    interaction.output_video = None
    return interaction


class TestLoadVideoBytesForEdit:
    def test_video_not_found_raises(self, test_db):
        with pytest.raises(video_edit_tools.OmniEditError, match="not found"):
            video_edit_tools._load_video_bytes_for_edit(999999)


class TestEditVideoWithOmni:
    @pytest.mark.asyncio
    async def test_success_creates_new_video_row(self, test_db):
        import base64

        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio, scene_prompt) "
                "VALUES (1, 1, 'source.mp4', 'generated', 8, '9:16', 'a scene')"
            )
            source_id = cursor.lastrowid

        fake_bytes = b"fake mp4 bytes"
        video_b64 = base64.b64encode(b"edited mp4 bytes").decode()

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=fake_bytes,
            ),
            patch(
                "app.tools.video_edit_tools.storage.save_video",
                return_value="omni-edit-x.mp4",
            ) as mock_save,
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = (
                _completed_interaction(video_b64)
            )
            mock_build_client.return_value = mock_client

            result = await video_edit_tools.edit_video_with_omni(
                source_id, "change the shirt color to red"
            )

        assert result["success"] is True
        assert result["source_video_id"] == source_id
        assert result["video_id"] != source_id
        assert result["backend"] == "omni_flash"
        assert result["interaction_id"] == "interaction-123"
        mock_save.assert_called_once()

        # Lineage persisted
        from app.database.db import get_db_cursor as _cur

        with _cur() as cursor:
            cursor.execute(
                "SELECT source_video_id FROM campaign_videos WHERE id = ?",
                (result["video_id"],),
            )
            row = cursor.fetchone()
        assert row["source_video_id"] == source_id

    @pytest.mark.asyncio
    async def test_missing_video_returns_error_dict(self, test_db):
        result = await video_edit_tools.edit_video_with_omni(
            999999, "change the color"
        )
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_non_completed_status_returns_unsupported_edit(self, test_db):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'source2.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        interaction = MagicMock()
        interaction.status = "failed"

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=b"x",
            ),
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = interaction
            mock_build_client.return_value = mock_client

            result = await video_edit_tools.edit_video_with_omni(
                source_id, "change the color"
            )

        assert result["success"] is False
        assert result["error_type"] == "unsupported_edit"

    @pytest.mark.asyncio
    async def test_missing_source_file_returns_storage_error_dict(
        self, test_db
    ):
        """A DB row whose asset isn't on disk must return an error dict, not
        raise storage.read_video()'s FileNotFoundError to the agent."""
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'missing-on-disk.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        with patch(
            "app.tools.video_edit_tools.storage.read_video",
            side_effect=FileNotFoundError("missing-on-disk.mp4"),
        ):
            result = await video_edit_tools.edit_video_with_omni(
                source_id, "change the color"
            )

        assert result["success"] is False
        assert result["error_type"] == "storage_error"

    @pytest.mark.asyncio
    async def test_cancelled_status_returns_api_error_not_unsupported(
        self, test_db
    ):
        """cancelled/budget_exceeded are not content-support problems --
        they must not be reported to the agent as 'unsupported edit'."""
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'source2b.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        interaction = MagicMock()
        interaction.status = "cancelled"

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=b"x",
            ),
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = interaction
            mock_build_client.return_value = mock_client

            result = await video_edit_tools.edit_video_with_omni(
                source_id, "change the color"
            )

        assert result["success"] is False
        assert result["error_type"] == "api_error"

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_error_type(self, test_db):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'source3.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        in_progress = MagicMock()
        in_progress.status = "in_progress"
        in_progress.id = "interaction-456"

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=b"x",
            ),
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
            patch("app.tools.video_edit_tools._POLL_INTERVAL_SECONDS", 0),
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = in_progress
            mock_client.interactions.get.return_value = (
                in_progress  # never reaches terminal
            )
            mock_build_client.return_value = mock_client

            result = await video_edit_tools.edit_video_with_omni(
                source_id, "change the color", max_wait_time=0
            )

        assert result["success"] is False
        assert result["error_type"] == "timeout"


class TestTranslateVideoDialoguePlaceholder:
    @pytest.mark.asyncio
    async def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await video_edit_tools.translate_video_dialogue(1, "es")


class TestContractDetailGuards:
    @pytest.mark.asyncio
    async def test_video_content_data_is_path_not_str(self, test_db):
        """Regression test for the live-discovered contract detail: a bare
        str for VideoContent.data silently sends corrupt base64 server-side.
        Assert the call site always passes a pathlib.Path.

        Note: google-genai's VideoContent.data field is typed
        Annotated[str, BeforeValidator(encode_base64_file_input)] -- pydantic
        always coerces the constructor argument to str at construction time
        (str in, str out; Path in, base64-encoded str out), so reading
        `.data`'s type back off an already-constructed VideoContent can never
        distinguish the two cases. Instead, spy on the VideoContent
        constructor call itself to capture the pre-validation argument type.
        """
        import base64
        from pathlib import Path

        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'source4.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        video_b64 = base64.b64encode(b"edited bytes").decode()
        captured = {}

        real_video_content = video_edit_tools.im.VideoContent

        def fake_video_content(*args, **kwargs):
            captured["data_value"] = kwargs.get("data")
            return real_video_content(*args, **kwargs)

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=b"x",
            ),
            patch(
                "app.tools.video_edit_tools.storage.save_video",
                return_value="out.mp4",
            ),
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
            patch(
                "app.tools.video_edit_tools.im.VideoContent",
                side_effect=fake_video_content,
            ),
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.return_value = (
                _completed_interaction(video_b64)
            )
            mock_build_client.return_value = mock_client

            await video_edit_tools.edit_video_with_omni(
                source_id, "change the color"
            )

        # Path (a PosixPath/WindowsPath instance in practice), never a bare str.
        assert isinstance(captured["data_value"], Path)
        assert not isinstance(captured["data_value"], str)

    @pytest.mark.asyncio
    async def test_response_format_never_sets_aspect_ratio(self, test_db):
        import base64

        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, product_id, video_filename, status, duration_seconds, aspect_ratio) "
                "VALUES (1, 1, 'source5.mp4', 'generated', 8, '9:16')"
            )
            source_id = cursor.lastrowid

        video_b64 = base64.b64encode(b"edited bytes").decode()
        captured = {}

        def fake_create(**kwargs):
            captured["response_format"] = kwargs["response_format"]
            return _completed_interaction(video_b64)

        with (
            patch(
                "app.tools.video_edit_tools.storage.read_video",
                return_value=b"x",
            ),
            patch(
                "app.tools.video_edit_tools.storage.save_video",
                return_value="out.mp4",
            ),
            patch(
                "app.tools.video_edit_tools._build_client"
            ) as mock_build_client,
        ):
            mock_client = MagicMock()
            mock_client.interactions.create.side_effect = fake_create
            mock_build_client.return_value = mock_client

            await video_edit_tools.edit_video_with_omni(
                source_id, "change the color"
            )

        assert captured["response_format"].aspect_ratio is None
