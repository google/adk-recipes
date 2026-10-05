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

// Command financial-advisor runs a multi-agent financial advisor. Run it with
// no arguments for an interactive console, or with "web api webui" for the ADK
// dev UI on http://localhost:8080.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"

	"google.golang.org/adk/agent"
	"google.golang.org/adk/cmd/launcher"
	"google.golang.org/adk/cmd/launcher/full"
)

func main() {
	ctx := context.Background()
	loadEnv()

	config, err := newLauncherConfig(ctx)
	if err != nil {
		log.Fatal(err)
	}

	l := full.NewLauncher()
	if err := l.Execute(ctx, config, os.Args[1:]); err != nil {
		log.Fatalf("Run failed: %v\n\n%s", err, l.CommandLineSyntax())
	}
}

// newLauncherConfig builds the agent from the environment and wraps it in the
// configuration the ADK launcher serves.
func newLauncherConfig(ctx context.Context) (*launcher.Config, error) {
	modelName := os.Getenv("MODEL_NAME")
	if modelName == "" {
		return nil, errors.New("environment variable MODEL_NAME is not set; copy .env.example to .env and fill it in")
	}

	rootAgent, err := newRootAgent(newLazyModel(ctx, modelName))
	if err != nil {
		return nil, fmt.Errorf("failed to create agent: %w", err)
	}
	return &launcher.Config{AgentLoader: agent.NewSingleLoader(rootAgent)}, nil
}
