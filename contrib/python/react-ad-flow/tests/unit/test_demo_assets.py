"""Phase 15 demo-asset bundle: sha256-manifest build/verify/install, no GCS
anywhere. Network download (gdown) is NOT tested — install is exercised via
--from-file, the same code path minus the fetch."""

import json
import zipfile

import pytest


@pytest.fixture
def source_dir(tmp_path):
    src = tmp_path / "assets-src"
    (src / "product-images").mkdir(parents=True)
    (src / "product-images" / "red-mug.png").write_bytes(
        b"\x89PNG" + b"a" * 500
    )
    (src / "product-images" / "blue-bottle.jpg").write_bytes(
        b"\xff\xd8" + b"b" * 500
    )
    return src


def test_build_writes_manifest_and_files(source_dir, tmp_path):
    from scripts.demo_assets import build_bundle

    out = tmp_path / "demo-assets.zip"
    result = build_bundle(str(source_dir), str(out))
    assert result["status"] == "success"
    assert result["file_count"] == 2
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["version"] == 1
    assert manifest["expected_count"] == 2
    assert manifest["files"]["product-images/red-mug.png"].startswith("sha256:")


def test_verify_and_extract_roundtrip(source_dir, tmp_path):
    from scripts.demo_assets import build_bundle, verify_and_extract

    out = tmp_path / "demo-assets.zip"
    build_bundle(str(source_dir), str(out))
    dest = tmp_path / "installed"
    result = verify_and_extract(str(out), str(dest))
    assert result["installed_count"] == 2
    assert (
        dest / "product-images" / "red-mug.png"
    ).read_bytes() == b"\x89PNG" + b"a" * 500


def test_tampered_file_rejected(source_dir, tmp_path):
    from scripts.demo_assets import build_bundle, verify_and_extract

    out = tmp_path / "demo-assets.zip"
    build_bundle(str(source_dir), str(out))
    # Rewrite one payload file with different bytes, keep the old manifest
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "product-images/red-mug.png":
                data = b"EVIL" + data
            zout.writestr(item, data)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        verify_and_extract(str(tampered), str(tmp_path / "dest2"))


def test_zip_slip_rejected(tmp_path):
    from scripts.demo_assets import verify_and_extract

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "files": {"../escape.txt": "sha256:00"},
                    "expected_count": 1,
                }
            ),
        )
        zf.writestr("../escape.txt", b"pwn")
    with pytest.raises(ValueError, match="unsafe path"):
        verify_and_extract(str(evil), str(tmp_path / "dest"))


def test_install_from_file_and_idempotence(source_dir, tmp_path, capsys):
    from scripts.demo_assets import build_bundle, install

    out = tmp_path / "demo-assets.zip"
    build_bundle(str(source_dir), str(out))
    dest = tmp_path / "assets-root"
    first = install(from_file=str(out), dest_dir=str(dest))
    assert first["status"] == "success"
    assert (dest / ".demo-assets-installed").exists()
    second = install(from_file=str(out), dest_dir=str(dest))
    assert second["status"] == "skipped"  # marker → idempotent


def test_install_without_drive_id_skips_gracefully(tmp_path, monkeypatch):
    from scripts.demo_assets import install

    monkeypatch.delenv("DEMO_ASSETS_DRIVE_ID", raising=False)
    result = install(dest_dir=str(tmp_path / "dest"))
    assert result["status"] == "skipped"
    assert "not configured" in result["message"]
