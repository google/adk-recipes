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
import static org.junit.jupiter.api.Assertions.assertInstanceOf;

import com.google.adk.agents.LlmAgent;
import com.google.adk.tools.BaseTool;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Runnability test: loading FinancialAdvisorAgent must build ROOT_AGENT and its four agent tools.
 * It never calls the model, so it needs no network access or credentials.
 */
class FinancialAdvisorAgentTest {

  @Test
  void rootAgentIsBuilt() {
    LlmAgent agent = assertInstanceOf(LlmAgent.class, FinancialAdvisorAgent.ROOT_AGENT);
    assertEquals("financial_coordinator", agent.name());
    assertEquals("financial_coordinator_output", agent.outputKey().orElseThrow());

    List<String> toolNames = agent.tools().blockingGet().stream().map(BaseTool::name).toList();
    assertEquals(
        List.of(
            "data_analyst_agent",
            "trading_analyst_agent",
            "execution_analyst_agent",
            "risk_analyst_agent"),
        toolNames);
  }
}
