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

import com.google.adk.agents.LlmAgent;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.Gemini;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.common.base.Supplier;
import com.google.common.base.Suppliers;
import com.google.genai.Client;
import io.github.cdimascio.dotenv.Dotenv;
import io.reactivex.rxjava3.core.Flowable;

/** Resolves the Gemini model shared by every agent from the environment or .env. */
final class Models {

  // Reads .env from the working directory. Real environment variables take
  // precedence, and a missing .env is fine (e.g. on Cloud Run).
  private static final Dotenv DOTENV = Dotenv.configure().ignoreIfMissing().load();

  private static final BaseLlm MODEL = new LazyGemini(requireEnv("MODEL_NAME"));

  private Models() {}

  /** Returns an agent builder with the shared model already set. */
  static LlmAgent.Builder agentBuilder() {
    return LlmAgent.builder().model(MODEL);
  }

  /**
   * Builds the GenAI client. The SDK only reads the process environment, so settings that come from
   * .env are passed to it explicitly; anything not set falls through to the SDK's defaults.
   */
  private static Client createClient() {
    Client.Builder client = Client.builder();
    if (Boolean.parseBoolean(env("GOOGLE_GENAI_USE_VERTEXAI"))) {
      client.vertexAI(true);
      String project = env("GOOGLE_CLOUD_PROJECT");
      if (project != null) {
        client.project(project);
      }
      String location = env("GOOGLE_CLOUD_LOCATION");
      if (location != null) {
        client.location(location);
      }
    } else {
      String apiKey = env("GOOGLE_API_KEY");
      if (apiKey != null) {
        client.apiKey(apiKey);
      }
    }
    return client.build();
  }

  /**
   * A Gemini model that builds its client on first use. Building a Vertex AI client looks up
   * credentials, so doing it while the agent loads would stop the dev server from loading the agent
   * at all when credentials are missing; deferring it turns that into an error on the first request
   * instead.
   */
  private static final class LazyGemini extends BaseLlm {
    private final Supplier<Gemini> delegate;

    LazyGemini(String modelName) {
      super(modelName);
      this.delegate =
          Suppliers.memoize(
              () -> Gemini.builder().modelName(modelName).apiClient(createClient()).build());
    }

    @Override
    public Flowable<LlmResponse> generateContent(LlmRequest llmRequest, boolean stream) {
      return delegate.get().generateContent(llmRequest, stream);
    }

    @Override
    public BaseLlmConnection connect(LlmRequest llmRequest) {
      return delegate.get().connect(llmRequest);
    }
  }

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
    String value = System.getenv(key);
    if (value == null) {
      value = DOTENV.get(key);
    }
    if (value == null || value.isBlank() || value.startsWith("<TODO")) {
      return null;
    }
    return value.trim();
  }
}
