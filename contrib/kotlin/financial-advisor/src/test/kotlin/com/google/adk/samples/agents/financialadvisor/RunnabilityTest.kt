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

import com.google.adk.kt.webserver.dev.AdkDevServer
import java.net.ServerSocket
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.test.Test
import kotlin.test.assertContains
import kotlin.test.assertEquals

/**
 * Runnability tests: the agent graph builds, and the dev server WebMain starts serves it. Neither
 * calls the model, so they need no network access or credentials.
 */
class RunnabilityTest {
    @Test
    fun rootAgentLoads() {
        val agent = FinancialAdvisorAgent.rootAgent
        assertEquals("financial_coordinator", agent.name)
        assertEquals("financial_coordinator_output", agent.outputKey)
        assertEquals(
            listOf("data_analyst_agent", "trading_analyst_agent", "execution_analyst_agent", "risk_analyst_agent"),
            agent.tools.map { it.name },
        )
    }

    @Test
    fun devServerServesTheAgent() {
        val port = ServerSocket(0).use { it.localPort }
        val server = AdkDevServer(devServerConfig { if (it == "PORT") port.toString() else null })
        server.start(wait = false)
        try {
            val response =
                HttpClient.newHttpClient().send(
                    HttpRequest.newBuilder(URI.create("http://127.0.0.1:$port/list-apps")).build(),
                    HttpResponse.BodyHandlers.ofString(),
                )
            assertEquals(200, response.statusCode())
            assertContains(response.body(), "\"financial_coordinator\"")
        } finally {
            server.stop()
        }
    }
}
