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

import { GOOGLE_SEARCH, LlmAgent } from "@google/adk";
import { DATA_ANALYST_PROMPT } from "./prompt";

/** Gathers recent market information about a ticker with Google Search. */
export const dataAnalystAgent = new LlmAgent({
  name: "data_analyst_agent",
  model: process.env.MODEL_NAME,
  instruction: DATA_ANALYST_PROMPT,
  outputKey: "market_data_analysis_output",
  tools: [GOOGLE_SEARCH],
});
