"""Archetype registry: maps product categories to prompt archetypes (ws09, Option B).

An archetype decides which prompt template family a product uses. Adding a new
vertical = one entry in ARCHETYPE_BY_CATEGORY (or nothing at all — unknown
categories fall back to the generic "product-hero"). Phase 15 may layer an LLM
prompt-writer behind the same resolve_archetype() seam for vendor onboarding.
"""

from ..models.product import Product
from ..models.variation import CreativeVariation

WEARABLE = "wearable"
CONSUMABLE_HERO = "consumable-hero"
STAGED_PRODUCT = "staged-product"
PRODUCT_HERO = "product-hero"

ARCHETYPE_BY_CATEGORY: dict[str, str] = {
    # fashion catalog (with-model path, preserved from the fashion-only era)
    "dress": WEARABLE,
    "top": WEARABLE,
    "pants": WEARABLE,
    "skirt": WEARABLE,
    "outerwear": WEARABLE,
    "footwear": WEARABLE,
    # retail core test set verticals (ws08)
    "beverage": CONSUMABLE_HERO,
    "qsr-menu-item": CONSUMABLE_HERO,
    "electronics": STAGED_PRODUCT,
    "furniture": STAGED_PRODUCT,
    "home-appliance": STAGED_PRODUCT,
}


def resolve_archetype(product: Product, variation: CreativeVariation) -> str:
    """Resolve which prompt archetype a (product, variation) pair uses.

    presentation_mode overrides the registry: "product_only" forces a
    product-centric shot even for wearables; "with_model" is only valid for
    wearable categories (person-using-product for other verticals is future
    work — see 09 phase doc exit criteria).
    """
    mapped = ARCHETYPE_BY_CATEGORY.get(product.category, PRODUCT_HERO)
    mode = variation.presentation_mode
    if mode in (None, "auto"):
        return mapped
    if mode == "product_only":
        return mapped if mapped != WEARABLE else PRODUCT_HERO
    if mode == "with_model":
        if mapped != WEARABLE:
            raise ValueError(
                f"presentation_mode 'with_model' is not supported for category "
                f"'{product.category}' (archetype '{mapped}') — only wearable "
                f"products support human-model shots in this version"
            )
        return WEARABLE
    raise ValueError(
        f"Invalid presentation_mode '{mode}' — valid values: auto, with_model, product_only"
    )
