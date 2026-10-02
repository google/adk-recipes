// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { AgentTool, App, LlmAgent } from "@google/adk";
import { FINANCIAL_COORDINATOR_PROMPT } from "./prompt";
import { dataAnalystAgent } from "./sub_agents/data_analyst/agent";
import { executionAnalystAgent } from "./sub_agents/execution_analyst/agent";
import { riskAnalystAgent } from "./sub_agents/risk_analyst/agent";
import { tradingAnalystAgent } from "./sub_agents/trading_analyst/agent";

// The ADK CLI loads .env into the environment before importing this file.
// Values still set to an unfilled .env.example placeholder are treated as
// unset, so a placeholder API key is never sent to the model.
for (const [key, value] of Object.entries(process.env)) {
  if (value?.startsWith("<TODO")) {
    delete process.env[key];
  }
}

/**
 * Financial coordinator: guides the user through market analysis, trading
 * strategies, an execution plan and a risk assessment by calling four
 * specialist agents as tools.
 */
export const rootAgent = new LlmAgent({
  name: "financial_coordinator",
  model: process.env.MODEL_NAME,
  description:
    "guide users through a structured process to receive financial " +
    "advice by orchestrating a series of expert subagents. help them " +
    "analyze a market ticker, develop trading strategies, define " +
    "execution plans, and evaluate the overall risk.",
  instruction: FINANCIAL_COORDINATOR_PROMPT,
  outputKey: "financial_coordinator_output",
  tools: [
    new AgentTool({ agent: dataAnalystAgent }),
    new AgentTool({ agent: tradingAnalystAgent }),
    new AgentTool({ agent: executionAnalystAgent }),
    new AgentTool({ agent: riskAnalystAgent }),
  ],
});

export const app = new App({ name: "app", rootAgent });
