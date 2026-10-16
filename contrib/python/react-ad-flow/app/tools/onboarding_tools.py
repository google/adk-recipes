"""Product onboarding tools (Phase 15): create products conversationally,
bulk-import from a local folder, and generate reference product images.

All three wrap db.insert_product(Product) — the typed write path — and route
image bytes through app.storage (the local-first seam). Wired to the Campaign
agent: onboarding is campaign-setup activity; catalog browsing stays on the
Media agent's list_products.
"""

import os
import re
import sqlite3

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

from .. import storage
from ..config import IMAGE_GENERATION
from ..database.db import (
    get_db_cursor,
    get_product,
    get_product_by_name,
    insert_product,
)
from ..models.product import Product

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_TINY_IMAGE_BYTES = (
    10 * 1024
)  # below this, warn (likely icon/corrupt) but import anyway


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "product"


def _image_status(filename: str) -> str:
    try:
        return (
            "available" if storage.product_image_exists(filename) else "pending"
        )
    except Exception:
        return "pending"


def _product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "description": product.description,
        "image_filename": product.image_filename,
        "image_status": _image_status(product.image_filename),
        "attributes": product.attributes,
    }


def create_product(
    name: str,
    category: str = "",
    description: str = "",
    attributes: dict | None = None,
) -> dict:
    """Create a single product in the catalog.

    The product is browse-ready immediately; its reference image starts as
    'pending' until an image is imported (import_products_from_folder) or
    generated (generate_product_image).

    Args:
        name: Unique product name (e.g. "Aurora Cold Brew 330ml").
        category: Vertical/category (e.g. beverage, footwear, dress).
        description: Short marketing-relevant description.
        attributes: Vertical-specific attributes (e.g. {"volume_ml": 330}).

    Returns:
        Dict with status, message, product (including image_status), next_steps.
    """
    if not name or not name.strip():
        return {"status": "error", "message": "Product name is required."}
    clean_name = name.strip()
    filename = f"{_slugify(clean_name)}.png"
    product = Product(
        name=clean_name,
        category=category or "",
        description=description or "",
        image_filename=filename,
        attributes=attributes or {},
    )
    try:
        stored = insert_product(product)
    except sqlite3.IntegrityError:
        return {
            "status": "error",
            "message": f"A product named '{clean_name}' already exists. Product names must be unique.",
        }
    return {
        "status": "success",
        "message": f"Product '{stored.name}' created (id={stored.id}).",
        "product": _product_payload(stored),
        "next_steps": (
            "Add a reference image: import_products_from_folder for existing photos, "
            f"or generate_product_image(product_id={stored.id}) to generate one. "
            f"Then create_campaign(product_id={stored.id}, store_name=..., city=..., state=...)."
        ),
    }


def import_products_from_folder(folder_path: str, category: str = "") -> dict:
    """Bulk-import products from a local folder of images.

    Each image file becomes one product: the filename stem (slugified) is the
    product name and the image is stored through the storage seam. Non-image
    files are ignored; duplicates are skipped; unusually small images are
    imported with a warning.

    Args:
        folder_path: Local folder containing .png/.jpg/.jpeg/.webp files.
        category: Category assigned to every imported product.

    Returns:
        Dict with status, message, created, skipped, warnings, next_steps.
    """
    folder = os.path.expanduser(folder_path)
    if not os.path.isdir(folder):
        return {
            "status": "error",
            "message": f"Folder not found: {folder_path}",
        }

    created, skipped, warnings = [], [], []
    for entry in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(entry)
        if ext.lower() not in _IMAGE_EXTENSIONS:
            continue
        with open(os.path.join(folder, entry), "rb") as f:
            data = f.read()
        if len(data) < _TINY_IMAGE_BYTES:
            warnings.append(
                f"{entry}: unusually small image ({len(data)} bytes) — imported anyway; verify it renders"
            )
        slug = _slugify(stem)
        filename = f"{slug}{ext.lower()}"
        product = Product(
            name=slug, category=category or "", image_filename=filename
        )
        try:
            stored = insert_product(product)
        except sqlite3.IntegrityError:
            skipped.append(f"{entry}: product '{slug}' already exists")
            continue
        path = storage.save_product_image(filename, data)
        _record_image_location(stored.id, path)
        created.append(
            {"id": stored.id, "name": stored.name, "image_filename": filename}
        )

    return {
        "status": "success",
        "message": f"Imported {len(created)} products from {folder_path} "
        f"({len(skipped)} skipped, {len(warnings)} warnings).",
        "created": created,
        "skipped": skipped,
        "warnings": warnings,
        "next_steps": "Products are browse-ready with stored images. "
        "Create campaigns with create_campaign(product_id=...).",
    }


def _record_image_location(product_id: int, path: str) -> None:
    """Record where the stored image landed (gcs_path or local_path column)."""
    column = "gcs_path" if storage.get_storage_mode() == "gcs" else "local_path"
    with get_db_cursor() as cursor:
        cursor.execute(
            f"UPDATE products SET {column} = ? WHERE id = ?",  # noqa: S608
            (path, product_id),
        )


async def generate_product_image(
    product_id: int = 0,
    product_name: str = "",
    style_hint: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Generate a reference product photo for a product with no image yet.

    Produces a clean, neutral-background catalog reference shot (category-aware
    prompt; deliberately NOT a creative scene — creative variation belongs to
    the video pipeline's archetype system). Stores the image through the
    storage seam under the product's image_filename.

    Args:
        product_id: Product ID (preferred lookup).
        product_name: Product name (used when product_id is 0).
        style_hint: Optional styling nudge (e.g. "warm morning light").
        tool_context: ADK tool context (injected) — used to render the image
            as an artifact in the chat UI.

    Returns:
        Dict with status, message, product (with image_status), next_steps.
    """
    product = get_product(product_id) if product_id else None
    if product is None and product_name:
        product = get_product_by_name(product_name)
    if product is None:
        return {
            "status": "error",
            "message": f"Product not found (product_id={product_id}, product_name={product_name!r}). "
            "Use list_products to browse the catalog.",
        }

    fragments = [f"Professional product photography of {product.name}"]
    if product.category:
        fragments.append(f"a {product.category}")
    if product.description:
        fragments.append(product.description)
    attr_text = ", ".join(
        f"{key}: {value}" for key, value in list(product.attributes.items())[:6]
    )
    prompt = ". ".join(fragments) + ". "
    if attr_text:
        prompt += f"Key attributes: {attr_text}. "
    if style_hint:
        prompt += f"Style: {style_hint}. "
    prompt += (
        "Clean neutral studio background, soft even lighting, the product centered "
        "and fully visible, catalog reference shot, no people, no text overlays."
    )

    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=IMAGE_GENERATION,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_modalities=["image", "text"]
            ),
        )
        image_bytes = None
        for part in response.candidates[0].content.parts:
            if getattr(part, "inline_data", None):
                image_bytes = part.inline_data.data
                break
    except Exception as e:
        return {"status": "error", "message": f"Image generation failed: {e}"}

    if not image_bytes:
        return {
            "status": "error",
            "message": "Image model returned no image — try again, or adjust style_hint.",
        }

    path = storage.save_product_image(product.image_filename, image_bytes)
    _record_image_location(product.id, path)

    if tool_context:
        artifact = types.Part.from_bytes(
            data=image_bytes, mime_type="image/png"
        )
        await tool_context.save_artifact(
            filename=product.image_filename, artifact=artifact
        )

    refreshed = get_product(product.id)
    return {
        "status": "success",
        "message": f"Reference image generated and stored for '{product.name}'.",
        "product": _product_payload(refreshed),
        "next_steps": f"create_campaign(product_id={product.id}, ...) to run this product, "
        "then generate_video_from_product for ad videos.",
    }
