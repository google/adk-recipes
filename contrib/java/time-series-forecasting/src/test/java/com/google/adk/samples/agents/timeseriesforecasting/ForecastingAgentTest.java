package com.google.adk.samples.agents.timeseriesforecasting;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import org.junit.jupiter.api.Test;

/**
 * Runnability test: loading ForecastingAgent must build ROOT_AGENT.
 *
 * <p>With MCP_TOOLBOX_SERVER_URL unset, the agent is built with no remote tools, so this needs no
 * MCP server, network access or credentials.
 */
class ForecastingAgentTest {

  @Test
  void rootAgentIsBuilt() {
    assertNotNull(ForecastingAgent.ROOT_AGENT);
    assertEquals("time-series-forecasting", ForecastingAgent.ROOT_AGENT.name());
  }
}
