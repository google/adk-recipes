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

"""Pydantic data models for brand search title comparison and evaluation."""

from pydantic import BaseModel, Field


class TitleRecommendation(BaseModel):
    """Specific title recommendation for a product."""

    original_title: str = Field(description="Original product catalog title")
    proposed_title: str = Field(
        description="Optimized product title recommendation"
    )
    keywords_added: list[str] = Field(
        default_factory=list,
        description="Specific keywords and attributes incorporated into the proposed title",
    )
    searchability_score: float = Field(
        ge=0.0,
        le=100.0,
        description="Searchability score from 0 to 100 for the proposed title",
    )
    rationale: str = Field(
        description="Explanation of why this title will perform better in retail search queries"
    )


class TitleOptimizationReport(BaseModel):
    """Comprehensive title optimization evaluation and recommendation report."""

    brand: str = Field(description="Brand name being optimized")
    primary_search_keyword: str = Field(
        description="Primary shopping keyword analyzed"
    )
    competitor_title_patterns: list[str] = Field(
        default_factory=list,
        description="Top competitor title patterns observed from search results",
    )
    keyword_gaps: list[str] = Field(
        default_factory=list,
        description="High-intent search terms missing from current catalog titles",
    )
    recommendations: list[TitleRecommendation] = Field(
        default_factory=list,
        description="Product-by-product title optimizations",
    )
    summary_findings: str = Field(
        description="Executive summary of search visibility findings and optimization impact"
    )
