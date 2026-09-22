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

import com.google.adk.kt.agents.SequentialAgent
import com.google.adk.kt.models.Gemini

/**
 * The LLM Auditor is a sequential multi-agent pipeline that fact-checks
 * LLM-generated answers. It chains two sub-agents:
 *
 * 1. **Critic**: Identifies claims in the answer, verifies each claim using
 *    Google Search, and produces an audit report with verdicts.
 * 2. **Reviser**: Takes the original answer and the critic's findings, then
 *    minimally edits the answer to correct any inaccuracies.
 */
object LlmAuditorAgent {
    private val model = Gemini(name = "gemini-flash-latest")

    @JvmField
    val rootAgent =
        SequentialAgent(
            name = "llm_auditor",
            description =
                "Evaluates and corrects LLM-generated answers by " +
                    "fact-checking claims and revising inaccuracies.",
            subAgents =
                listOf(
                    createCriticAgent(model),
                    createReviserAgent(model),
                ),
        )
}
