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

package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"google.golang.org/adk/artifact"
	"google.golang.org/adk/server/adkrest"
	"google.golang.org/adk/session"
)

// TestRunnability builds the launcher configuration main serves and checks
// that the ADK REST API, the handler behind "web api", lists the agent. It
// never calls the model, so it needs no network access or credentials.
func TestRunnability(t *testing.T) {
	if _, ok := os.LookupEnv("MODEL_NAME"); !ok {
		t.Setenv("MODEL_NAME", "gemini-3.5-flash")
	}

	config, err := newLauncherConfig(context.Background())
	if err != nil {
		t.Fatalf("newLauncherConfig() error = %v", err)
	}

	server, err := adkrest.NewServer(adkrest.ServerConfig{
		SessionService:  session.InMemoryService(),
		ArtifactService: artifact.InMemoryService(),
		AgentLoader:     config.AgentLoader,
	})
	if err != nil {
		t.Fatalf("adkrest.NewServer() error = %v", err)
	}
	ts := httptest.NewServer(server)
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/list-apps")
	if err != nil {
		t.Fatalf("GET /list-apps error = %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("reading /list-apps body: %v", err)
	}

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /list-apps status = %d, want %d; body: %s", resp.StatusCode, http.StatusOK, body)
	}
	if !strings.Contains(string(body), `"financial_coordinator"`) {
		t.Errorf("GET /list-apps body = %s, want it to list financial_coordinator", body)
	}
}
