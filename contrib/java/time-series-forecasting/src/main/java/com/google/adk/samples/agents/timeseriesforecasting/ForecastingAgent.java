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

package com.google.adk.samples.agents.timeseriesforecasting;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.Gemini;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.sessions.Session;
import com.google.adk.tools.mcp.McpToolset;
import com.google.adk.tools.mcp.SseServerParameters;
import com.google.common.collect.ImmutableList;
import com.google.genai.Client;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionResponse;
import com.google.genai.types.Part;
import io.github.cdimascio.dotenv.Dotenv;
import io.reactivex.rxjava3.core.Flowable;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Scanner;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Level;
import java.util.logging.Logger;

/** The main application class for the time series forecasting agent. */
public class ForecastingAgent {
  private static final Logger ADK_LOGGER = Logger.getLogger(ForecastingAgent.class.getName());

  private static final String AGENT_NAME = "time-series-forecasting";
  private static final String MCP_TOOLBOX_SERVER_URL_ENV_VAR = "MCP_TOOLBOX_SERVER_URL";

  // Reads .env from the working directory. Real environment variables take
  // precedence, and a missing .env is fine (e.g. on Cloud Run).
  private static final Dotenv DOTENV = Dotenv.configure().ignoreIfMissing().load();

  private static final String MODEL_NAME = requireEnv("MODEL_NAME");

  public static final BaseAgent ROOT_AGENT = initAgent();

  /** Returns a required variable from the environment or .env, failing fast when it is unset. */
  private static String requireEnv(String key) {
    String value = env(key);
    if (value == null) {
      throw new IllegalStateException(
          key + " is not set. Copy .env.example to .env and fill it in, or export " + key + ".");
    }
    return value;
  }

  /**
   * Returns a variable from the environment or .env, or null if it is unset, blank, or still an
   * unfilled .env.example placeholder.
   */
  private static String env(String key) {
    String value = DOTENV.get(key);
    if (value == null || value.isBlank() || value.startsWith("<TODO")) {
      return null;
    }
    return value.trim();
  }

  /**
   * Returns a Vertex AI model when the Vertex settings are configured, else null. The GenAI SDK
   * only reads the process environment, so settings that come from .env are passed to the client
   * explicitly.
   */
  private static BaseLlm getVertexModel() {
    String project = env("GOOGLE_CLOUD_PROJECT");
    String location = env("GOOGLE_CLOUD_LOCATION");
    if (!Boolean.parseBoolean(env("GOOGLE_GENAI_USE_VERTEXAI"))
        || project == null
        || location == null) {
      return null;
    }
    Client client = Client.builder().vertexAI(true).project(project).location(location).build();
    return Gemini.builder().modelName(MODEL_NAME).apiClient(client).build();
  }

  /**
   * Returns the MCP Toolbox toolset, or no tools when MCP_TOOLBOX_SERVER_URL is unset. The toolset
   * connects to the server and lists its tools when the agent first needs them.
   */
  private static List<Object> getTools() {
    String mcpServerUrl = env(MCP_TOOLBOX_SERVER_URL_ENV_VAR);
    if (mcpServerUrl == null) {
      ADK_LOGGER.info(
          MCP_TOOLBOX_SERVER_URL_ENV_VAR
              + " environment variable not set. No remote tools will be loaded.");
      return ImmutableList.of();
    }
    ADK_LOGGER.info("Using MCP Toolbox server: " + mcpServerUrl);
    return ImmutableList.of(
        new McpToolset(SseServerParameters.builder().url(mcpServerUrl).build()));
  }

  /**
   * Creates a time series forecasting agent.
   *
   * @return The created LLM agent.
   */
  private static BaseAgent initAgent() {
    List<Object> tools = getTools();
    LlmAgent.Builder builder = LlmAgent.builder();
    BaseLlm vertexModel = getVertexModel();
    if (vertexModel != null) {
      builder.model(vertexModel);
    } else {
      builder.model(MODEL_NAME);
    }
    return builder
        .name(AGENT_NAME)
        .description(
            "A general-purpose agent that performs time series forecasting using provided tools.")
        .instruction(
            """
            You are a highly skilled expert at time-series forecasting, possessing strong data science skills. You will be provided with tools to solve specific time series problems.

            Your general process is as follows:

            1.  **Understand the User Request:** Carefully analyze the user's request to determine the forecasting goal (e.g., "forecast Iowa liquor sales for 7 days").
            2.  **Identify the Appropriate Tool:** Select the most suitable forecasting tool from the available tools (e.g., forecastIowaLiquorSalesTool) based on the request.
            3.  **Determine Parameters:**
                *   Based on the context provided by the user, the tool metadata, and your general understanding of the problem, identify the required parameters for the selected tool.
                *   Pay close attention to units. If the user specifies a duration like "7 days" and the 'horizon' parameter is in hours, convert the duration to hours (7 days * 24 hours/day = 168 hours).
            4.  **Validate Parameters:** Before calling the tool, double-check that all required parameters are present and valid. If any parameters are missing or invalid, inform the user and ask for clarification.
            5.  **Call the Tool:** Once the parameters are validated, call the tool with the determined parameters.
            6.  **Analyze Results and Provide Insights:**
                *   If the tool returns a successful forecast, provide the full forecast details in a human-readable format.
                *   **Crucially, leverage your data science expertise to provide qualitative analysis and insights.** This should include:
                    *   Identifying key trends and patterns in the forecast data.
                    *   Explaining the potential drivers behind these trends, drawing upon your knowledge of the domain.
                    *   Discussing the limitations of the forecast and potential sources of error.
                    *   Suggesting potential actions or decisions based on the forecast and your insights.
                *   If an error occurs or the tool returns an error message, inform the user clearly about what happened and what the tool returned (or that it returned nothing).
            7.  **Output Forecast Data:** Make sure to output the complete and detailed forecast data as provided by the forecasting tool, along with your qualitative analysis and insights.

            Refer to the specific names and descriptions of the tools provided to you to determine their requirements and parameters.
            """)
        .tools(tools)
        .build();
  }

  public static void main(String[] args) {
    ADK_LOGGER.setLevel(Level.WARNING);

    InMemoryRunner runner = new InMemoryRunner(ROOT_AGENT);
    Session session =
        runner
            .sessionService()
            .createSession(
                ROOT_AGENT.name(), "tmp-user", (ConcurrentMap<String, Object>) null, (String) null)
            .blockingGet();

    runInteractiveSession(runner, session, ROOT_AGENT);
  }

  private static void runInteractiveSession(
      InMemoryRunner runner, Session session, BaseAgent agent) {
    System.out.println("\\nTime Series Forecasting Agent");
    System.out.println("-----------------------------");
    System.out.println("Examples:");
    System.out.println("predict next week's liquor sales in iowa");
    System.out.println("how many SF bike trips are expected tomorrow");
    System.out.println("forecast seattle air quality for the next 10 days");

    try (Scanner scanner = new Scanner(System.in, StandardCharsets.UTF_8)) {
      while (true) {
        System.out.print("\\nYou > ");
        String userInput = scanner.nextLine();

        if ("quit".equalsIgnoreCase(userInput.trim())) {
          break;
        }
        if (userInput.trim().isEmpty()) {
          continue;
        }

        Content userMsgForHistory = Content.fromParts(Part.fromText(userInput));
        Flowable<Event> events =
            runner.runAsync(
                session.userId(), session.id(), userMsgForHistory, RunConfig.builder().build());

        System.out.print("\\nAgent > ");
        final StringBuilder agentResponseBuilder = new StringBuilder();
        final AtomicBoolean toolCalledInTurn = new AtomicBoolean(false);
        final AtomicBoolean toolErroredInTurn = new AtomicBoolean(false);

        events.blockingForEach(
            event ->
                processAgentEvent(
                    event, agentResponseBuilder, toolCalledInTurn, toolErroredInTurn));

        System.out.println();

        if (toolCalledInTurn.get()
            && !toolErroredInTurn.get()
            && agentResponseBuilder.length() == 0) {
          ADK_LOGGER.warning("Agent used a tool but provided no text response.");
        } else if (toolErroredInTurn.get()) {
          ADK_LOGGER.warning(
              "An error occurred during tool execution or in the agent's response processing.");
        }
      }
    }
    System.out.println("Exiting agent.");
  }

  private static void processAgentEvent(
      Event event,
      StringBuilder agentResponseBuilder,
      AtomicBoolean toolCalledInTurn,
      AtomicBoolean toolErroredInTurn) {
    if (event.content().isPresent()) {
      event
          .content()
          .get()
          .parts()
          .ifPresent(
              parts -> {
                for (Part part : parts) {
                  if (part.text().isPresent()) {
                    System.out.print(part.text().get());
                    agentResponseBuilder.append(part.text().get());
                  }
                  if (part.functionCall().isPresent()) {
                    toolCalledInTurn.set(true);
                  }
                  if (part.functionResponse().isPresent()) {
                    FunctionResponse fr = part.functionResponse().get();
                    fr.response()
                        .ifPresent(
                            responseMap -> {
                              if (responseMap.containsKey("error")
                                  || (responseMap.containsKey("status")
                                      && "error"
                                          .equalsIgnoreCase(
                                              String.valueOf(responseMap.get("status"))))) {
                                toolErroredInTurn.set(true);
                              }
                            });
                  }
                }
              });
    }
    if (event.errorCode().isPresent() || event.errorMessage().isPresent()) {
      toolErroredInTurn.set(true);
    }
  }
}
