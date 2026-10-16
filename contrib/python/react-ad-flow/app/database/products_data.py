# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Product catalog data - 22 pre-generated product images.

These products are the source of truth for the ad campaign agent.
Product images were generated using scripts/generate_product_images.py
and stored in scripts/products/.

Each product has:
- name: Hyphenated product name (e.g., 'emerald-satin-slip-dress')
- category: Product category (dress, pants, skirt, top, outerwear)
- style: Specific style description
- color: Primary color(s)
- fabric: Material type
- details: Key features and details
- occasion: Suggested use occasions
- image_filename: PNG filename
- local_path: Path to the image file
"""

from typing import Any

PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "black-high-waist-trousers",
        "category": "pants",
        "style": "tailored wide-leg trousers",
        "color": "classic black",
        "fabric": "wool blend suiting",
        "details": "high waist, pleated front, wide straight leg, side pockets",
        "occasion": "office and professional wear",
        "image_filename": "black-high-waist-trousers.png",
        "local_path": "scripts/products/black-high-waist-trousers.png",
    },
    {
        "name": "black-leather-moto-jacket",
        "category": "outerwear",
        "style": "motorcycle jacket",
        "color": "black",
        "fabric": "genuine leather",
        "details": "asymmetric zip, notched lapels, zip pockets, belted waist",
        "occasion": "edgy casual and evening looks",
        "image_filename": "black-leather-moto-jacket.png",
        "local_path": "scripts/products/black-leather-moto-jacket.png",
    },
    {
        "name": "black-pleated-midi-skirt",
        "category": "skirt",
        "style": "accordion pleated midi skirt",
        "color": "black",
        "fabric": "flowy chiffon",
        "details": "elastic waist, all-around pleats, midi length, lined",
        "occasion": "office to evening transition",
        "image_filename": "black-pleated-midi-skirt.png",
        "local_path": "scripts/products/black-pleated-midi-skirt.png",
    },
    {
        "name": "blue-floral-maxi-dress",
        "category": "dress",
        "style": "tiered maxi dress",
        "color": "blue and yellow floral on white",
        "fabric": "lightweight cotton",
        "pattern": "botanical floral print",
        "details": "smocked bodice, spaghetti straps, three-tier skirt",
        "occasion": "vacation and resort wear",
        "image_filename": "blue-floral-maxi-dress.png",
        "local_path": "scripts/products/blue-floral-maxi-dress.png",
    },
    {
        "name": "blue-floral-summer-dress",
        "category": "dress",
        "style": "tiered midi dress",
        "color": "blue and yellow florals on white",
        "fabric": "lightweight cotton",
        "pattern": "botanical floral print",
        "details": "smocked bodice, spaghetti straps, three-tier skirt",
        "image_filename": "blue-floral-summer-dress.png",
        "local_path": "scripts/products/blue-floral-summer-dress.png",
    },
    {
        "name": "blush-ruffle-peplum-blouse",
        "category": "top",
        "style": "feminine peplum blouse",
        "color": "blush pink",
        "fabric": "crepe de chine",
        "details": "ruffled v-neck, short sleeves, flared peplum hem",
        "occasion": "romantic dinners and special occasions",
        "image_filename": "blush-ruffle-peplum-blouse.png",
        "local_path": "scripts/products/blush-ruffle-peplum-blouse.png",
    },
    {
        "name": "camel-pleated-culottes",
        "category": "pants",
        "style": "cropped pleated culottes",
        "color": "warm camel",
        "fabric": "flowy crepe",
        "details": "high waist, front pleats, wide cropped leg, back zip",
        "occasion": "smart casual and brunch",
        "image_filename": "camel-pleated-culottes.png",
        "local_path": "scripts/products/camel-pleated-culottes.png",
    },
    {
        "name": "camel-wool-overcoat",
        "category": "outerwear",
        "style": "classic wool overcoat",
        "color": "camel tan",
        "fabric": "wool-cashmere blend",
        "details": "notched lapels, double-breasted, knee-length, side pockets",
        "occasion": "fall and winter professional wear",
        "image_filename": "camel-wool-overcoat.png",
        "local_path": "scripts/products/camel-wool-overcoat.png",
    },
    {
        "name": "cream-linen-palazzo-pants",
        "category": "pants",
        "style": "flowing palazzo pants",
        "color": "natural cream",
        "fabric": "lightweight linen",
        "details": "elastic waist, ultra-wide leg, relaxed drape",
        "occasion": "resort and summer casual",
        "image_filename": "cream-linen-palazzo-pants.png",
        "local_path": "scripts/products/cream-linen-palazzo-pants.png",
    },
    {
        "name": "denim-a-line-mini-skirt",
        "category": "skirt",
        "style": "classic A-line mini skirt",
        "color": "light blue wash",
        "fabric": "cotton denim",
        "details": "high waist, front button closure, side pockets, frayed hem",
        "occasion": "casual summer outings",
        "image_filename": "denim-a-line-mini-skirt.png",
        "local_path": "scripts/products/denim-a-line-mini-skirt.png",
    },
    {
        "name": "dusty-rose-blazer",
        "category": "outerwear",
        "style": "tailored single-button blazer",
        "color": "dusty rose pink",
        "fabric": "stretch suiting",
        "details": "notched lapels, single button, flap pockets, fitted silhouette",
        "occasion": "office and smart casual",
        "image_filename": "dusty-rose-blazer.png",
        "local_path": "scripts/products/dusty-rose-blazer.png",
    },
    {
        "name": "elegant-black-cocktail-dress",
        "category": "dress",
        "style": "fitted cocktail dress",
        "color": "classic black",
        "fabric": "stretch crepe",
        "details": "square neckline, cap sleeves, back zipper, knee-length",
        "occasion": "evening events and parties",
        "image_filename": "elegant-black-cocktail-dress.png",
        "local_path": "scripts/products/elegant-black-cocktail-dress.png",
    },
    {
        "name": "emerald-satin-slip-dress",
        "category": "dress",
        "style": "bias-cut slip dress",
        "color": "rich emerald green",
        "fabric": "luxurious satin",
        "details": "cowl neckline, adjustable straps, midi length, subtle sheen",
        "occasion": "formal dinners and date nights",
        "image_filename": "emerald-satin-slip-dress.png",
        "local_path": "scripts/products/emerald-satin-slip-dress.png",
    },
    {
        "name": "floral-midi-wrap-dress",
        "category": "dress",
        "style": "midi wrap dress",
        "color": "pink and white",
        "fabric": "flowing chiffon",
        "pattern": "delicate floral print",
        "details": "v-neck, flutter sleeves, self-tie waist, tiered skirt",
        "occasion": "summer events and brunch",
        "image_filename": "floral-midi-wrap-dress.png",
        "local_path": "scripts/products/floral-midi-wrap-dress.png",
    },
    {
        "name": "indigo-straight-leg-jeans",
        "category": "pants",
        "style": "classic straight-leg jeans",
        "color": "medium indigo wash",
        "fabric": "premium stretch denim",
        "details": "high waist, five-pocket styling, straight leg, subtle fading",
        "occasion": "everyday casual wear",
        "image_filename": "indigo-straight-leg-jeans.png",
        "local_path": "scripts/products/indigo-straight-leg-jeans.png",
    },
    {
        "name": "ivory-lace-crochet-top",
        "category": "top",
        "style": "bohemian crochet top",
        "color": "ivory cream",
        "fabric": "cotton crochet lace",
        "pattern": "intricate floral crochet",
        "details": "scalloped edges, short sleeves, relaxed fit",
        "occasion": "beach cover-up and summer festivals",
        "image_filename": "ivory-lace-crochet-top.png",
        "local_path": "scripts/products/ivory-lace-crochet-top.png",
    },
    {
        "name": "leopard-print-pencil-skirt",
        "category": "skirt",
        "style": "fitted pencil skirt",
        "color": "brown and black leopard print",
        "fabric": "stretch ponte",
        "pattern": "classic leopard print",
        "details": "high waist, back zip, back vent, knee-length",
        "occasion": "bold office looks and nights out",
        "image_filename": "leopard-print-pencil-skirt.png",
        "local_path": "scripts/products/leopard-print-pencil-skirt.png",
    },
    {
        "name": "navy-striped-breton-top",
        "category": "top",
        "style": "classic Breton striped top",
        "color": "navy and white stripes",
        "fabric": "soft cotton jersey",
        "pattern": "horizontal stripes",
        "details": "boat neckline, three-quarter sleeves, relaxed fit",
        "occasion": "casual everyday wear",
        "image_filename": "navy-striped-breton-top.png",
        "local_path": "scripts/products/navy-striped-breton-top.png",
    },
    {
        "name": "olive-cargo-joggers",
        "category": "pants",
        "style": "modern cargo joggers",
        "color": "olive green",
        "fabric": "soft twill cotton",
        "details": "elastic waist, side cargo pockets, tapered ankle, drawstring hem",
        "occasion": "casual weekend wear",
        "image_filename": "olive-cargo-joggers.png",
        "local_path": "scripts/products/olive-cargo-joggers.png",
    },
    {
        "name": "rust-boho-peasant-dress",
        "category": "dress",
        "style": "bohemian peasant dress",
        "color": "warm rust orange",
        "fabric": "textured cotton gauze",
        "pattern": "subtle embroidered details",
        "details": "square neckline, balloon sleeves, smocked waist, midi length",
        "occasion": "festivals and casual outings",
        "image_filename": "rust-boho-peasant-dress.png",
        "local_path": "scripts/products/rust-boho-peasant-dress.png",
    },
    {
        "name": "sage-satin-camisole",
        "category": "top",
        "style": "drapey camisole",
        "color": "soft sage green",
        "fabric": "silky satin",
        "details": "cowl neckline, adjustable straps, relaxed fit",
        "occasion": "evening wear layering",
        "image_filename": "sage-satin-camisole.png",
        "local_path": "scripts/products/sage-satin-camisole.png",
    },
    {
        "name": "white-classic-button-blouse",
        "category": "top",
        "style": "tailored button-down blouse",
        "color": "crisp white",
        "fabric": "cotton poplin",
        "details": "pointed collar, French cuffs, slightly relaxed fit",
        "occasion": "office and professional settings",
        "image_filename": "white-classic-button-blouse.png",
        "local_path": "scripts/products/white-classic-button-blouse.png",
    },
]
