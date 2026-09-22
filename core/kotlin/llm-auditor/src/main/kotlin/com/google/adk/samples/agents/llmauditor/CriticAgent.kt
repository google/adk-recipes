/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.adk.samples.agents.llmauditor

import com.google.adk.kt.agents.Instruction
import com.google.adk.kt.agents.LlmAgent
import com.google.adk.kt.models.Gemini
import com.google.adk.kt.tools.GoogleSearchTool

/**
 * Creates the critic sub-agent that fact-checks LLM responses using Google
 * Search. It identifies claims, verifies them against external sources, and
 * produces a structured audit report with verdicts for each claim.
 */
fun createCriticAgent(model: Gemini): LlmAgent =
    LlmAgent(
        name = "critic_agent",
        description = "Fact-checks claims in an answer using Google Search.",
        model = model,
        instruction = Instruction(CRITIC_PROMPT),
        tools = listOf(GoogleSearchTool()),
    )
