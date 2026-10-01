/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.adk.samples.agents.financialadvisor

import com.google.adk.kt.models.Gemini
import com.google.adk.kt.models.LlmRequest
import com.google.adk.kt.models.LlmResponse
import com.google.adk.kt.models.Model
import com.google.adk.kt.models.VertexCredentials
import kotlinx.coroutines.flow.Flow
import java.io.File

/** Resolves the Gemini model shared by every agent from the environment or `.env`. */
internal object Models {
    /**
     * Returns the model named by `MODEL_NAME`. Its client is created on first use: creating a
     * Vertex AI client looks up credentials, so doing it while the agent loads would fail the whole
     * server when credentials are missing, instead of the first request.
     */
    fun fromEnvironment(): Model {
        val name =
            env("MODEL_NAME")
                ?: error("MODEL_NAME is not set. Copy .env.example to .env and fill it in, or export MODEL_NAME.")
        return LazyModel(name) {
            if (env("GOOGLE_GENAI_USE_VERTEXAI").toBoolean()) {
                val project =
                    env("GOOGLE_CLOUD_PROJECT")
                        ?: error("GOOGLE_CLOUD_PROJECT is not set. Set it in .env or the environment to use Vertex AI.")
                // A null location lets the SDK use its default, "global".
                Gemini(name, VertexCredentials(project, env("GOOGLE_CLOUD_LOCATION")))
            } else {
                Gemini(name, env("GOOGLE_API_KEY"))
            }
        }
    }

    // Reads .env from the working directory. Real environment variables take
    // precedence, and a missing .env is fine (e.g. on Cloud Run).
    private val dotenv: Map<String, String> by lazy {
        val file = File(".env")
        if (!file.isFile) return@lazy emptyMap()
        file
            .readLines()
            .map { it.substringBefore(" #").trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") && "=" in it }
            .associate { it.substringBefore("=").trim() to it.substringAfter("=").trim() }
    }

    /**
     * Returns a variable from the environment or `.env`, or null if it is unset, blank, or still an
     * unfilled `.env.example` placeholder.
     */
    private fun env(key: String): String? =
        (System.getenv(key) ?: dotenv[key])?.trim()?.takeUnless { it.isEmpty() || it.startsWith("<TODO") }

    private class LazyModel(
        override val name: String,
        create: () -> Model,
    ) : Model {
        private val delegate by lazy(create)

        override fun generateContent(
            request: LlmRequest,
            stream: Boolean,
        ): Flow<LlmResponse> = delegate.generateContent(request, stream)
    }
}
