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
import com.google.adk.kt.callbacks.AfterModelCallback
import com.google.adk.kt.models.Gemini
import com.google.adk.kt.models.LlmResponse
import com.google.adk.kt.types.Part

/**
 * After-model callback that strips the "---END-OF-EDIT---" marker and any
 * content after it from the reviser's response.
 */
private val removeEndOfEditMark =
    AfterModelCallback { _, response ->
        val content = response.content ?: return@AfterModelCallback response
        val parts = content.parts

        val trimmedParts = mutableListOf<Part>()
        for (part in parts) {
            val text = part.text
            if (text != null && text.contains(END_MARK)) {
                trimmedParts.add(part.copy(text = text.substringBefore(END_MARK).trimEnd()))
                break
            }
            trimmedParts.add(part)
        }

        response.copy(content = content.copy(parts = trimmedParts))
    }

/**
 * Creates the reviser sub-agent that rewrites inaccurate answers based on the
 * critic's findings. It minimally edits the original text to correct errors
 * while preserving structure and style.
 */
fun createReviserAgent(model: Gemini): LlmAgent =
    LlmAgent(
        name = "reviser_agent",
        description = "Revises answers based on the critic's fact-check findings.",
        model = model,
        instruction = Instruction(REVISER_PROMPT),
        afterModelCallbacks = listOf(removeEndOfEditMark),
    )
