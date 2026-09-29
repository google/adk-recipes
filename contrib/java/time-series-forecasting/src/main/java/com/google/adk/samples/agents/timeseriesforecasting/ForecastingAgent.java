package com.google.adk.samples.agents.timeseriesforecasting;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Scanner;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Level;
import java.util.logging.Logger;
import java.util.stream.Collectors;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.google.adk.agents.BaseAgent;
import com.google.adk.events.Event;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.RunConfig;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.sessions.Session;
import com.google.adk.tools.BaseTool;
import com.google.adk.tools.mcp.McpToolset;
import com.google.adk.tools.mcp.SseServerParameters;
import com.google.common.collect.ImmutableList;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionResponse;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;

/**
 * The main application class for the time series forecasting agent.
 */
public class ForecastingAgent {
    private static final Logger ADK_LOGGER = Logger.getLogger(ForecastingAgent.class.getName());

    private static final String AGENT_NAME = "time-series-forecasting";
    private static final String MODEL_NAME = "gemini-2.5-flash";
    private static final String MCP_TOOLBOX_SERVER_URL_ENV_VAR = "MCP_TOOLBOX_SERVER_URL";

    public static final BaseAgent ROOT_AGENT = initAgent();

    /**
     * Loads tools from the MCP server.
     *
     * @return The list of tools.
     */
    private static List<BaseTool> getTools() {
        List<BaseTool> tools = ImmutableList.of();

        String mcpServerUrl = System.getenv(MCP_TOOLBOX_SERVER_URL_ENV_VAR);
        ADK_LOGGER.info("MCP Server URL from env: " + mcpServerUrl);

        if (mcpServerUrl == null || mcpServerUrl.trim().isEmpty()) {
            ADK_LOGGER.info(MCP_TOOLBOX_SERVER_URL_ENV_VAR
                    + " environment variable not set. No remote tools will be loaded.");
        } else {
            ADK_LOGGER.info("Attempting to load tools from MCP server: " + mcpServerUrl);

            try {
                SseServerParameters params =
                        SseServerParameters.builder().url(mcpServerUrl).build();
                ADK_LOGGER.fine("URL in SseServerParameters object: " + params.url());

                McpToolset.McpToolsAndToolsetResult toolsAndToolsetResult =
                        McpToolset.fromServer(params, new ObjectMapper()).get();

                if (toolsAndToolsetResult == null) {
                    ADK_LOGGER.warning("Failed to load tools from MCP server at " + mcpServerUrl
                            + ". Load method returned null.");
                } else {
                    McpToolset toolset =
                            (toolsAndToolsetResult != null) ? toolsAndToolsetResult.getToolset()
                                    : null;
                    try (McpToolset managedToolset = toolset) {
                        if (toolsAndToolsetResult != null
                                && toolsAndToolsetResult.getTools() != null) {
                            tools = toolsAndToolsetResult.getTools().stream()
                                    .collect(Collectors.toList());
                            ADK_LOGGER.info("Loaded " + tools.size() + " tools.");
                        } else {
                            tools = ImmutableList.of();
                            ADK_LOGGER.warning(
                                    "Proceeding with an empty tool list due to previous errors or no tools loaded.");
                        }

                        if (tools.isEmpty()
                                && System.getenv(MCP_TOOLBOX_SERVER_URL_ENV_VAR) != null) {
                            ADK_LOGGER.warning(MCP_TOOLBOX_SERVER_URL_ENV_VAR
                                    + " was set, but no tools were loaded. Agent will function without these tools.");
                        } else if (tools.isEmpty()) {
                            ADK_LOGGER.warning("No tools are configured for the agent.");
                        }
                    }
                }
            } catch (Exception e) {
                ADK_LOGGER.log(Level.WARNING, "Failed to load tools from MCP server at "
                        + mcpServerUrl
                        + ". Ensure the server is running and accessible, and the URL is correct.",
                        e);
            }
        }

        return tools;
    }

    /**
     * Creates a time series forecasting agent.
     *
     * @return The created LLM agent.
     */
    private static BaseAgent initAgent() {
        List<BaseTool> tools = getTools();
        return LlmAgent.builder().name(AGENT_NAME).description(
                "A general-purpose agent that performs time series forecasting using provided tools.")
                .model(MODEL_NAME)
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
                .tools(tools).build();
    }

    public static void main(String[] args) {
        ADK_LOGGER.setLevel(Level.WARNING);

        InMemoryRunner runner = new InMemoryRunner(ROOT_AGENT);
        Session session = runner.sessionService().createSession(ROOT_AGENT.name(), "tmp-user",
                (ConcurrentMap<String, Object>) null, (String) null).blockingGet();

        runInteractiveSession(runner, session, ROOT_AGENT);

    }

    private static void runInteractiveSession(InMemoryRunner runner, Session session,
            BaseAgent agent) {
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
                Flowable<Event> events = runner.runWithSessionId(session.id(), userMsgForHistory,
                        RunConfig.builder().build());

                System.out.print("\\nAgent > ");
                final StringBuilder agentResponseBuilder = new StringBuilder();
                final AtomicBoolean toolCalledInTurn = new AtomicBoolean(false);
                final AtomicBoolean toolErroredInTurn = new AtomicBoolean(false);

                events.blockingForEach(event -> processAgentEvent(event, agentResponseBuilder,
                        toolCalledInTurn, toolErroredInTurn));

                System.out.println();

                if (toolCalledInTurn.get() && !toolErroredInTurn.get()
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

    private static void processAgentEvent(Event event, StringBuilder agentResponseBuilder,
            AtomicBoolean toolCalledInTurn, AtomicBoolean toolErroredInTurn) {
        if (event.content().isPresent()) {
            event.content().get().parts().ifPresent(parts -> {
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
                        fr.response().ifPresent(responseMap -> {
                            if (responseMap.containsKey("error")
                                    || (responseMap.containsKey("status")
                                            && "error".equalsIgnoreCase(
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
