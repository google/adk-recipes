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

package com.google.adk.samples.agents.financialadvisor;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.google.adk.web.AdkWebServer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;

/**
 * Runnability tests: the agent loads, and the ADK dev server the Dockerfile runs discovers and
 * serves it. Neither calls the model, so they need no network access or credentials.
 */
class RunnabilityTest {

  @Test
  void agentLoads() {
    assertNotNull(FinancialAdvisorAgent.ROOT_AGENT);
  }

  @Test
  void devServerServesTheAgent() throws Exception {
    // The Dockerfile's entry point and arguments, on a free port. The server scans
    // target/classes for ROOT_AGENT, which is what the container does too.
    try (ConfigurableApplicationContext server =
        SpringApplication.run(AdkWebServer.class, "--server.port=0", "--adk.agents.source-dir=.")) {
      String port = server.getEnvironment().getProperty("local.server.port");
      HttpResponse<String> response =
          HttpClient.newHttpClient()
              .send(
                  HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/list-apps"))
                      .build(),
                  HttpResponse.BodyHandlers.ofString());

      assertEquals(200, response.statusCode());
      assertTrue(response.body().contains("\"financial_coordinator\""), response.body());
    }
  }
}
