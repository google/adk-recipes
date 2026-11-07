"""Demo asset bundle: build, verify, install (Phase 15).

Local-first replacement for pulling demo product images from a personal GCS
bucket: assets ship as an owner-hosted Google Drive zip, auto-downloadable at
setup. No GCS anywhere in this path (ws09 directive).

  python -m scripts.demo_assets build --source DIR --out demo-assets.zip
  python -m scripts.demo_assets install [--from-file demo-assets.zip]

install resolves the bundle from --from-file, else downloads by the
DEMO_ASSETS_DRIVE_ID env var (gdown); with neither, it skips gracefully.
Contents are verified against the bundled manifest.json (sha256 per file)
before landing in LOCAL_ASSETS_DIR; a marker file makes re-runs no-ops.

Owner workflow to (re)publish the bundle: collect the demo product images
into a folder tree (product-images/<file>...), run `build`, upload the zip to
Google Drive (anyone-with-link), set DEMO_ASSETS_DRIVE_ID to the file ID.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ADK only loads .env when serving the agent; standalone runs (make
# demo-assets, the pre-`make dev` install hook) need it loaded here so a
# DEMO_ASSETS_DRIVE_ID set in .env is honored. Shell env still wins —
# load_dotenv never overrides existing variables.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.config import LOCAL_ASSETS_DIR  # noqa: E402

MANIFEST_NAME = "manifest.json"
MARKER_NAME = ".demo-assets-installed"
BUNDLE_VERSION = 1


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_bundle(source_dir: str, out_path: str) -> dict:
    """Zip every file under source_dir (relative paths preserved) + manifest."""
    source = Path(source_dir)
    if not source.is_dir():
        return {
            "status": "error",
            "message": f"Source folder not found: {source_dir}",
        }
    files = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            data = path.read_bytes()
            files[rel] = _sha256(data)
            zf.writestr(rel, data)
        manifest = {
            "version": BUNDLE_VERSION,
            "files": files,
            "expected_count": len(files),
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    return {"status": "success", "bundle": out_path, "file_count": len(files)}


def verify_and_extract(zip_path: str, dest_dir: str) -> dict:
    """Verify every bundled file against manifest.json, then extract to dest.

    Raises ValueError on a missing manifest, count mismatch, sha256
    mismatch, or unsafe (absolute / ..) paths. Nothing is written unless the
    whole bundle verifies.
    """
    dest = Path(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME))
        except KeyError:
            raise ValueError(f"Bundle has no {MANIFEST_NAME}") from None
        files = manifest.get("files", {})
        if len(files) != manifest.get("expected_count"):
            raise ValueError(
                "manifest expected_count does not match its file map"
            )
        verified = {}
        for rel, expected in files.items():
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise ValueError(f"unsafe path in bundle: {rel}")
            data = zf.read(rel)
            actual = _sha256(data)
            if actual != expected:
                raise ValueError(
                    f"sha256 mismatch for {rel}: {actual} != {expected}"
                )
            verified[rel] = data
    for rel, data in verified.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {
        "status": "success",
        "installed_count": len(verified),
        "dest": str(dest),
    }


def _download_from_drive(drive_id: str, out_path: str) -> str:
    import gdown  # lazy: only the download path needs it

    return str(gdown.download(id=drive_id, output=out_path, quiet=False))


def install(from_file: str | None = None, dest_dir: str | None = None) -> dict:
    """Install the demo asset bundle into dest_dir (default LOCAL_ASSETS_DIR)."""
    dest = Path(dest_dir or LOCAL_ASSETS_DIR)
    marker = dest / MARKER_NAME
    if marker.exists():
        return {
            "status": "skipped",
            "message": f"Demo assets already installed ({marker}). Delete the marker to force reinstall.",
        }

    if from_file:
        zip_path = from_file
    else:
        drive_id = (os.environ.get("DEMO_ASSETS_DRIVE_ID") or "").strip()
        if not drive_id:
            return {
                "status": "skipped",
                "message": "Demo asset bundle not configured (DEMO_ASSETS_DRIVE_ID unset "
                "and no --from-file). Skipping — the app works without it; "
                "seeded products will show image_status=missing locally.",
            }
        fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="demo-assets-")
        os.close(fd)
        _download_from_drive(drive_id, zip_path)

    result = verify_and_extract(zip_path, str(dest))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": BUNDLE_VERSION,
                "installed_count": result["installed_count"],
            }
        )
    )
    return {
        "status": "success",
        "message": f"Installed {result['installed_count']} demo asset files into {dest}.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or install the demo asset bundle"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build", help="Build a bundle zip from a source folder"
    )
    p_build.add_argument("--source", required=True)
    p_build.add_argument("--out", required=True)

    p_install = sub.add_parser("install", help="Verify + install a bundle")
    p_install.add_argument(
        "--from-file",
        default=None,
        help="Use a local bundle zip instead of downloading from Drive",
    )
    p_install.add_argument(
        "--dest", default=None, help="Install root (default: LOCAL_ASSETS_DIR)"
    )

    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_bundle(args.source, args.out)
    else:
        result = install(from_file=args.from_file, dest_dir=args.dest)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
