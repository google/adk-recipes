/*
 * Copyright 2026 Google LLC
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

import static java.nio.charset.StandardCharsets.UTF_8;

import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.RunConfig;
import com.google.adk.events.Event;
import com.google.adk.runner.FirestoreDatabaseRunner;
import com.google.adk.sessions.FirestoreSessionService;
import com.google.adk.sessions.Session;
import com.google.adk.sessions.SessionNotFoundException;
import com.google.adk.tools.Annotations.Schema;
import com.google.adk.tools.FunctionTool;
import com.google.cloud.firestore.Firestore;
import com.google.cloud.firestore.FirestoreOptions;
import com.google.genai.types.Content;
import com.google.genai.types.Part;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.core.Maybe;
import java.util.Map;
import java.util.Optional;
import java.util.Scanner;
import java.util.UUID;
import java.util.logging.Logger;

/**
 * Entry point demonstrating Firestore-backed persistent session state with ADK Java SDK. A simple
 * time-agent converses with the user and retains session history in Firestore across turns.
 */
public final class FirestoreRunner {

  private static final Logger logger = Logger.getLogger(FirestoreRunner.class.getName());
  private static final String APP_NAME = "time-agent";

  public static void main(String[] args) {
    logger.info("Starting FirestoreRunner...");

    RunConfig runConfig = RunConfig.builder().build();

    BaseAgent timeAgent = initAgent(APP_NAME);

    try (Firestore firestore = FirestoreOptions.getDefaultInstance().getService();
        Scanner scanner = new Scanner(System.in, UTF_8)) {

      FirestoreDatabaseRunner runner = new FirestoreDatabaseRunner(timeAgent, APP_NAME, firestore);
      FirestoreSessionService sessionService = new FirestoreSessionService(firestore);

      String userId = System.getenv("USER_ID");
      String sessionId = System.getenv("SESSION_ID");

      Session session;
      if (userId != null && !userId.isBlank() && sessionId != null && !sessionId.isBlank()) {
        String resumeUserId = userId;
        String resumeSessionId = sessionId;
        session =
            sessionService
                .getSession(APP_NAME, resumeUserId, resumeSessionId, Optional.empty())
                .onErrorResumeNext(
                    error ->
                        error instanceof SessionNotFoundException
                            ? sessionService
                                .createSession(APP_NAME, resumeUserId, null, resumeSessionId)
                                .toMaybe()
                            : Maybe.error(error))
                .blockingGet();
        logger.info("Resumed session " + sessionId + " for user " + userId);
      } else {
        userId = "user-" + UUID.randomUUID();
        sessionId = "session-" + UUID.randomUUID();
        session = sessionService.createSession(APP_NAME, userId, null, sessionId).blockingGet();
      }

      System.out.println(
          "\nSession ready. To resume this conversation later, restart with:\n"
              + "USER_ID="
              + userId
              + " SESSION_ID="
              + sessionId);

      while (true) {
        System.out.print("\nYou > ");
        if (!scanner.hasNextLine()) {
          break;
        }
        String userInput = scanner.nextLine();
        if ("quit".equalsIgnoreCase(userInput) || "exit".equalsIgnoreCase(userInput)) {
          break;
        }

        Content userMsg = Content.fromParts(Part.fromText(userInput));
        Flowable<Event> events =
            runner.runAsync(session.userId(), session.id(), userMsg, runConfig);

        System.out.print("\nAgent > ");
        events.blockingForEach(
            event -> {
              if (event.finalResponse()) {
                System.out.println(event.stringifyContent());
              }
            });
      }
    } catch (Exception e) {
      logger.severe("An error occurred: " + e.getMessage());
    }

    logger.info("FirestoreRunner finished execution.");
    // The Gemini API client leaves non-daemon OkHttp threads running; force
    // a clean exit instead of waiting on them to time out.
    System.exit(0);
  }

  @Schema(description = "Get the current time for a given city")
  public static Map<String, String> getCurrentTime(
      @Schema(name = "city", description = "Name of the city to get the time for") String city) {
    return Map.of("city", city, "time", "The time is 10:30 am.");
  }

  protected static BaseAgent initAgent(String agentName) {
    String modelEnv = System.getenv("MODEL_NAME");
    String model = (modelEnv != null && !modelEnv.isBlank()) ? modelEnv : "gemini-3.5-flash";
    return LlmAgent.builder()
        .name(agentName)
        .description("Tells the current time in a specified city")
        .instruction(
            """
            You are a helpful assistant that tells the current time in a city.
            Use the 'getCurrentTime' tool for this purpose.
            """)
        .model(model)
        .tools(FunctionTool.create(FirestoreRunner.class, "getCurrentTime"))
        .build();
  }
}
