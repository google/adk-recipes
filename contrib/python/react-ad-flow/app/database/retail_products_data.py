"""Multi-vertical retail core test set (Phase 8).

Proof that the product schema is vertical-agnostic: SKUs across five
unrelated retail verticals ("anything sellable on BlueZoo in-store
screens" — owner decision at the workstream-08 gate), all attributes-first
(no fashion columns). Image files are referenced but not yet generated —
Phase 14a/15 add upload / nano-banana generation paths that grow this core
set into real imagery. Seeded additively; the fashion demo catalog in
products_data.py is untouched.
"""

from typing import Any

RETAIL_TEST_PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "aurora-cold-brew-330ml",
        "category": "beverage",
        "description": "slow-steeped single-origin cold brew coffee in a slim can",
        "image_filename": "aurora-cold-brew-330ml.png",
        "attributes": {
            "flavor": "dark chocolate and toasted hazelnut notes",
            "volume_ml": 330,
            "packaging": "slim aluminum can",
            "serving_temperature": "chilled",
            "caffeine_mg": 180,
            "dietary_tags": ["vegan", "zero-sugar"],
        },
    },
    {
        "name": "citrus-grove-sparkling-water-500ml",
        "category": "beverage",
        "description": "unsweetened sparkling water with real citrus zest",
        "image_filename": "citrus-grove-sparkling-water-500ml.png",
        "attributes": {
            "flavor": "grapefruit and blood orange",
            "volume_ml": 500,
            "packaging": "glass bottle",
            "serving_temperature": "chilled",
            "caffeine_mg": 0,
            "dietary_tags": ["zero-calorie", "sodium-free"],
        },
    },
    {
        "name": "smoky-brisket-stack-sandwich",
        "category": "qsr-menu-item",
        "description": "12-hour smoked brisket with pickled onions on a brioche bun",
        "image_filename": "smoky-brisket-stack-sandwich.png",
        "attributes": {
            "cuisine": "texas barbecue",
            "calories": 780,
            "spice_level": "medium",
            "key_ingredients": [
                "smoked brisket",
                "pickled red onion",
                "brioche bun",
            ],
            "combo_options": ["fries + drink", "coleslaw + drink"],
            "limited_time_window": "summer 2026",
        },
    },
    {
        "name": "pulse-anc-wireless-earbuds",
        "category": "electronics",
        "description": "active noise cancelling wireless earbuds with wireless charging case",
        "image_filename": "pulse-anc-wireless-earbuds.png",
        "attributes": {
            "brand": "Pulse Audio",
            "battery_life_hours": 32,
            "connectivity": "bluetooth 5.4",
            "noise_cancellation": "adaptive ANC",
            "finish": "matte graphite",
            "warranty_months": 24,
        },
    },
    {
        "name": "nordic-oak-lounge-chair",
        "category": "furniture",
        "description": "mid-century lounge chair in solid oak with wool boucle cushions",
        "image_filename": "nordic-oak-lounge-chair.png",
        "attributes": {
            "material": "solid oak, wool boucle upholstery",
            "dimensions_cm": {"width": 72, "depth": 80, "height": 76},
            "weight_capacity_kg": 150,
            "assembly_required": True,
            "style_family": "mid-century scandinavian",
        },
    },
    {
        "name": "crispwave-air-fryer-5l",
        "category": "home-appliance",
        "description": "5-liter digital air fryer with eight one-touch presets",
        "image_filename": "crispwave-air-fryer-5l.png",
        "attributes": {
            "capacity_liters": 5,
            "wattage": 1700,
            "presets": ["fries", "wings", "roast", "bake", "reheat"],
            "dishwasher_safe_parts": True,
            "warranty_months": 12,
        },
    },
]
