# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

SHELL := /bin/bash
export PROJECT_ID ?= $(GOOGLE_CLOUD_PROJECT)
export REGION ?= us-central1

# Boolean config flags to toggle skill surfacing personas across both retail skills
export SURFACE_GEMINI_SKILL ?= true
export SURFACE_AGENTS_SKILL ?= true

surface: surface-product-search surface-virtual-tryon

surface-product-search:
	@$(MAKE) -C skills/product-search surface

surface-virtual-tryon:
	@$(MAKE) -C skills/virtual-tryon surface

setup-product-search:
	@$(MAKE) -C skills/product-search setup

setup-virtual-tryon:
	@$(MAKE) -C skills/virtual-tryon setup

test:
	cd skills/product-search && uv run pytest
	cd skills/virtual-tryon && uv run pytest

.PHONY: surface surface-product-search surface-virtual-tryon setup-product-search setup-virtual-tryon test
