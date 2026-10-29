# Copyright 2026 Google LLC
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

"""The job spec the Studio agent's ``generate`` starts, from the turn's
working state (``studio.turn.current().st``)."""

from __future__ import annotations

from typing import Any

from genmedia4commerce.studio import turn

__all__ = ["job_spec"]


def job_spec(cap: str) -> tuple[dict[str, Any], str]:
    """The job spec from the working state, and what is missing ("" if none).
    A video try-on always starts from the person and the products."""
    sc = turn.current()
    st = sc.st

    def ref(x: Any) -> str:
        return x.get("ref", "") if isinstance(x, dict) else ""

    settings = st.get("settings") or {}
    spec = {
        "capability": cap,
        "studio_id": sc.studio_id,
        "person": ref(st.get("person")),
        "reference_model": ref(st.get("reference_model")),
        "products": [ref(p) for p in st.get("products") or [] if ref(p)],
        "spin": [ref(p) for p in st.get("spin") or [] if ref(p)],
        "settings": {**settings.get("tryon", {}), **settings.get(cap, {})},
    }
    if cap == "spin":
        return spec, "" if spec["spin"] else "photos of the product to spin"
    missing = [
        name
        for name, ok in (
            ("a person", spec["person"]),
            ("a product", spec["products"]),
        )
        if not ok
    ]
    return spec, " and ".join(missing)
