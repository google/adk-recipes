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
"""Unit tests for recipe_images_matrix.py.

Everything this module admits becomes a publicly pullable image, and
everything it drops is a recipe with no image at all. Both directions are
silent, so these tests pin the admission rules rather than the happy path.
"""

from pathlib import Path

import recipe_images_matrix as m


def _recipe(tmp_path: Path, rel: str, *, manifest: bool = True) -> Path:
    """A directory with a Dockerfile, and by default a manifest beside it."""
    d = tmp_path / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    if manifest:
        (d / "manifest.yaml").write_text(
            'language: "python"\n', encoding="utf-8"
        )
    return d


def _paths(found: list[dict[str, str]]) -> list[str]:
    return [f["path"] for f in found]


def test_finds_recipe_with_dockerfile_and_manifest(tmp_path: Path) -> None:
    _recipe(tmp_path, "core/python/alpha")
    assert _paths(m.discover(tmp_path)) == ["core/python/alpha"]


def test_skips_dockerfile_without_manifest(tmp_path: Path) -> None:
    """A Dockerfile alone is a build asset, not a recipe.

    skills/retail/virtual-tryon/assets/export-template is the real instance:
    containerized, but nothing anybody deploys as an agent.
    """
    _recipe(
        tmp_path, "skills/retail/thing/assets/export-template", manifest=False
    )
    assert m.discover(tmp_path) == []


def test_skips_retired_roots(tmp_path: Path) -> None:
    """python/agents and java/agents are frozen_paths in .github/policy.yml.

    They still carry Dockerfiles. Publishing from them would ship images built
    from code that pull requests are not allowed to fix.
    """
    _recipe(tmp_path, "python/agents/legacy")
    _recipe(tmp_path, "java/agents/legacy")
    assert m.discover(tmp_path) == []


def test_nested_dockerfile_is_a_sub_service_not_a_recipe(
    tmp_path: Path,
) -> None:
    """Only the recipe-root Dockerfile serves the agent.

    contrib/python/multiformat-hybrid-rag ships four; three belong to
    data-ingestion services with their own lifecycles.
    """
    _recipe(tmp_path, "contrib/python/rag")
    _recipe(tmp_path, "contrib/python/rag/ingestion")
    _recipe(tmp_path, "contrib/python/rag/ingestion/chunker")
    assert _paths(m.discover(tmp_path)) == ["contrib/python/rag"]


def test_nested_exclusion_survives_a_sub_service_that_sorts_first(
    tmp_path: Path,
) -> None:
    """Regression: the parent must be recorded before anything beneath it.

    Lexicographic order does not give that. 'rag/Bar/Dockerfile' sorts ahead
    of 'rag/Dockerfile' because 'B' < 'D', which let the sub-service register
    as a recipe of its own and publish an image.
    """
    _recipe(tmp_path, "contrib/python/rag")
    _recipe(tmp_path, "contrib/python/rag/Bar")
    assert _paths(m.discover(tmp_path)) == ["contrib/python/rag"]


def test_image_name_drops_the_root_segment(tmp_path: Path) -> None:
    """Promotion from contrib/ to core/ must not rename the image.

    Consumers pin these paths; the recipe is the same recipe either side of
    the move.
    """
    _recipe(tmp_path, "contrib/python/alpha")
    contrib = m.discover(tmp_path)[0]["image"]

    (tmp_path / "contrib").rename(tmp_path / "core")
    core = m.discover(tmp_path)[0]["image"]

    assert contrib == core
    assert core == f"{m.REGISTRY}/python/alpha"


def test_category_disambiguates_same_named_recipes(tmp_path: Path) -> None:
    """core/kotlin/llm-auditor and contrib/python/llm-auditor coexist today."""
    _recipe(tmp_path, "core/kotlin/llm-auditor")
    _recipe(tmp_path, "contrib/python/llm-auditor")
    images = {f["image"] for f in m.discover(tmp_path)}
    assert images == {
        f"{m.REGISTRY}/kotlin/llm-auditor",
        f"{m.REGISTRY}/python/llm-auditor",
    }


def test_skip_dirs_are_pruned(tmp_path: Path) -> None:
    _recipe(tmp_path, "core/python/alpha/.venv/vendored")
    assert m.discover(tmp_path) == []


def test_changed_paths_returns_none_when_the_diff_fails(tmp_path: Path) -> None:
    """None and the empty set drive opposite decisions upstream.

    A failed diff must rebuild everything; a clean diff that found nothing
    must rebuild nothing. Collapsing both to an empty set makes the second
    case behave like the first.
    """
    assert m._changed_paths("no-such-ref", tmp_path) is None


def test_self_paths_cover_both_files_that_define_the_matrix() -> None:
    """A push touching only these matches no recipe prefix.

    Without the escape they would filter down to an empty matrix and merge
    having built nothing, which is precisely when a build is most wanted.
    """
    assert m.SELF_PATHS == {
        ".github/scripts/recipe_images_matrix.py",
        ".github/workflows/recipe-images.yml",
    }
