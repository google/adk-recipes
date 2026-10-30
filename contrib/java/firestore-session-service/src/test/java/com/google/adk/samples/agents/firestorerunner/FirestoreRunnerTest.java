/*
 * Copyright 2025 Google LLC
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
package com.google.adk.samples.agents.firestorerunner;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.LlmAgent;
import com.google.adk.tools.BaseTool;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Unit tests for the FirestoreRunner class. */
public class FirestoreRunnerTest {

  @Test
  void testGetCurrentTime() {
    Map<String, String> result = FirestoreRunner.getCurrentTime("London");
    assertNotNull(result);
    assertEquals(2, result.size());
    assertTrue(result.containsKey("city"));
    assertTrue(result.containsKey("forecast"));
    assertEquals("London", result.get("city"));
    assertEquals("The time is 10:30 am.", result.get("forecast"));
  }

  @Test
  void testInitAgent() {
    BaseAgent agent = FirestoreRunner.initAgent("time-agent");
    assertNotNull(agent);
    assertTrue(agent instanceof LlmAgent);

    LlmAgent llmAgent = (LlmAgent) agent;
    assertEquals("time-agent", llmAgent.name());
    assertEquals("Tells the current time in a specified city", llmAgent.description());
    assertTrue(llmAgent.model().isPresent());

    List<BaseTool> tools = llmAgent.tools().blockingGet();
    assertNotNull(tools);
    assertEquals(1, tools.size());
    assertEquals("getCurrentTime", tools.get(0).name());
  }
}
