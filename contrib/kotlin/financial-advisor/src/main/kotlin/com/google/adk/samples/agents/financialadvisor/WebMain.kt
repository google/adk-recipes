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

package com.google.adk.samples.agents.financialadvisor

import com.google.adk.kt.webserver.AdkServerConfig
import com.google.adk.kt.webserver.dev.AdkDevServer

fun main() {
    val config = devServerConfig()
    println("Starting ADK dev server on http://${config.host}:${config.port}")
    AdkDevServer(config).start(wait = true)
}

/**
 * Config for serving the agent. inMemory() supplies the agent loader and the session and artifact
 * services, holding their state in the process. It binds 127.0.0.1:8080; HOST and PORT override
 * that, so a container can bind 0.0.0.0 on the port it is given.
 */
fun devServerConfig(env: (String) -> String? = System::getenv): AdkServerConfig {
    var config = AdkServerConfig.inMemory(FinancialAdvisorAgent.rootAgent)
    env("HOST")?.let { config = config.copy(host = it) }
    env("PORT")?.let { config = config.copy(port = it.toInt()) }
    return config
}
