"""Doc 15 step 5: a product onboarded via the new tools attaches to the
analytics chain with NO fixture data — campaign-id-keyed attribution derives
deterministic metrics and a finite RPI for a never-before-seen vertical."""

from datetime import date

import pytest


@pytest.fixture
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.GCS_BUCKET", None)
    monkeypatch.setattr(
        "app.config.PRODUCT_IMAGES_DIR", str(tmp_path / "product-images")
    )
    return tmp_path


def test_onboarded_product_flows_to_rpi(empty_test_db, local_mode):
    from app.database.db import get_db_cursor
    from app.demo_data.derive import derive_video_metrics_rows
    from app.tools.campaign_tools import create_campaign
    from app.tools.metrics_shared import compute_rpi
    from app.tools.onboarding_tools import create_product

    created = create_product(
        name="Aurora Cold Brew 330ml",
        category="beverage",
        description="Nitro cold brew in a slim can",
    )
    assert created["status"] == "success"
    product_id = created["product"]["id"]

    campaign = create_campaign(
        product_id=product_id,
        store_name="Demo Store",
        city="Austin",
        state="TX",
    )
    assert campaign["status"] == "success"
    campaign_id = campaign["campaign"]["id"]

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO campaign_videos
            (campaign_id, product_id, video_filename, status)
            VALUES (?, ?, 'aurora-demo.mp4', 'activated')
        """,
            (campaign_id, product_id),
        )
        video_id = cursor.lastrowid

    date_from, date_to = date(2026, 6, 1), date(2026, 6, 7)
    rows_a = derive_video_metrics_rows(
        campaign_id, [video_id], date_from, date_to
    )
    rows_b = derive_video_metrics_rows(
        campaign_id, [video_id], date_from, date_to
    )
    assert rows_a, (
        "expected derived metric rows for the onboarded product's video"
    )
    assert rows_a == rows_b, "derivation must be deterministic"

    total_impressions = sum(r["impressions"] for r in rows_a)
    total_revenue = sum(r["revenue"] for r in rows_a)
    assert total_impressions > 0
    rpi = compute_rpi(total_revenue, total_impressions)
    assert rpi > 0
