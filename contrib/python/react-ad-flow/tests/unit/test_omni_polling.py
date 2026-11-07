"""Tests for Gemini Omni Flash polling and video extraction helpers."""

import pytest

from app.tools import video_tools


class FakeOmniInteraction:
    def __init__(
        self,
        id="omni-test-id",
        completes_after=0,
        status="completed",
        data=b"mp4-bytes",
    ):
        self.id = id
        self._polls_remaining = completes_after
        self.status = "in_progress" if completes_after > 0 else status
        self._final_status = status
        self.output_video = type("V", (), {"data": data})()
        self.steps = []


def make_omni_client(interaction: FakeOmniInteraction):
    client = type("FakeClient", (), {"polls": 0})()

    class Interactions:
        @staticmethod
        def create(**kw):
            return interaction

        @staticmethod
        def get(interaction_id, **kw):
            client.polls += 1
            interaction._polls_remaining -= 1
            if interaction._polls_remaining <= 0:
                interaction.status = interaction._final_status
            return interaction

    client.interactions = Interactions()
    return client


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(video_tools.asyncio, "sleep", _no_sleep)


class TestWaitForOmniInteraction:
    @pytest.mark.asyncio
    async def test_success_after_polls(self):
        interaction = FakeOmniInteraction(completes_after=3)
        client = make_omni_client(interaction)
        done = await video_tools._wait_for_omni_interaction(client, interaction)
        assert done.status == "completed"
        assert client.polls == 3

    @pytest.mark.asyncio
    async def test_already_done_no_polls(self):
        interaction = FakeOmniInteraction(completes_after=0)
        client = make_omni_client(interaction)
        done = await video_tools._wait_for_omni_interaction(client, interaction)
        assert client.polls == 0
        assert done is interaction

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        interaction = FakeOmniInteraction(completes_after=10_000)
        client = make_omni_client(interaction)
        with pytest.raises(TimeoutError, match="timed out after 600"):
            await video_tools._wait_for_omni_interaction(client, interaction)
        # 600s budget / 10s interval = 60 polls
        assert client.polls == 60

    @pytest.mark.asyncio
    async def test_failed_status_raises_valueerror(self):
        interaction = FakeOmniInteraction(completes_after=1, status="failed")
        client = make_omni_client(interaction)
        with pytest.raises(ValueError, match="failed with status: failed"):
            await video_tools._wait_for_omni_interaction(client, interaction)


class TestExtractOmniVideoBytes:
    def test_extract_from_output_video_bytes(self):
        interaction = FakeOmniInteraction(data=b"hello-video")
        out = video_tools._extract_omni_video_bytes(interaction)
        assert out == b"hello-video"

    def test_extract_from_output_video_base64_str(self):
        import base64

        b64 = base64.b64encode(b"hello-video-str").decode("ascii")
        interaction = FakeOmniInteraction(data=b64)
        out = video_tools._extract_omni_video_bytes(interaction)
        assert out == b"hello-video-str"

    def test_empty_video_raises(self):
        interaction = FakeOmniInteraction()
        interaction.output_video = None
        with pytest.raises(ValueError, match="contained no video content"):
            video_tools._extract_omni_video_bytes(interaction)


class TestGenerateVideoAdTimeoutSemantics:
    """generate_video_ad must keep its DB-update + error-dict timeout behavior
    (it maps the helper's exceptions at the call site instead of raising)."""

    @pytest.mark.asyncio
    async def test_timeout_marks_ad_failed_and_returns_error(
        self, fresh_test_db, monkeypatch
    ):
        import io as _io

        from PIL import Image as PILImage

        # Minimal valid PNG for the PIL sniff in generate_video_ad
        buf = _io.BytesIO()
        PILImage.new("RGB", (4, 4)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaigns (name, city, state, category) VALUES (?, ?, ?, ?)",
                ("omni-timeout-test", "Los Angeles", "CA", "holiday"),
            )
            campaign_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO campaign_images (campaign_id, image_path, metadata) VALUES (?, ?, ?)",
                (campaign_id, "omni-timeout-test.png", "{}"),
            )

        monkeypatch.setattr(
            video_tools.storage, "get_storage_mode", lambda: "local"
        )
        monkeypatch.setattr(
            video_tools.storage, "get_image_path", lambda f: f"/tmp/{f}"
        )
        monkeypatch.setattr(video_tools.storage, "image_exists", lambda f: True)
        monkeypatch.setattr(
            video_tools.storage, "read_image", lambda f: png_bytes
        )

        never_done = FakeOmniInteraction(completes_after=10_000)
        fake_client = make_omni_client(never_done)
        monkeypatch.setattr(video_tools.genai, "Client", lambda: fake_client)

        result = await video_tools.generate_video_ad(
            campaign_id=campaign_id,
            custom_prompt="test prompt",
            duration_seconds=4,
        )

        assert result["status"] == "error"
        assert "timed out" in result["message"]
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT status FROM campaign_ads WHERE id = ?",
                (result["ad_id"],),
            )
            assert cursor.fetchone()["status"] == "failed"
