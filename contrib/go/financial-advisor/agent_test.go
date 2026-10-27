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
	"slices"
	"testing"
)

// The tests build the agent graph without calling the model, so they need no
// network access or credentials.

func TestRootAgent(t *testing.T) {
	rootAgent, err := newRootAgent(newLazyModel(context.Background(), "gemini-3.5-flash"))
	if err != nil {
		t.Fatalf("newRootAgent() error = %v", err)
	}
	if got := rootAgent.Name(); got != "financial_coordinator" {
		t.Errorf("Name() = %q, want %q", got, "financial_coordinator")
	}
}

func TestSpecialistTools(t *testing.T) {
	tools, err := newSpecialistTools(newLazyModel(context.Background(), "gemini-3.5-flash"))
	if err != nil {
		t.Fatalf("newSpecialistTools() error = %v", err)
	}
	var names []string
	for _, tl := range tools {
		names = append(names, tl.Name())
	}
	want := []string{"data_analyst_agent", "trading_analyst_agent", "execution_analyst_agent", "risk_analyst_agent"}
	if !slices.Equal(names, want) {
		t.Errorf("tool names = %v, want %v", names, want)
	}
}
