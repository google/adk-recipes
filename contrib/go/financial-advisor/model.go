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
	"iter"
	"os"
	"strings"
	"sync"

	"github.com/joho/godotenv"
	"google.golang.org/adk/model"
	"google.golang.org/adk/model/gemini"
	"google.golang.org/genai"
)

// loadEnv reads .env from the working directory into the process environment,
// where the GenAI SDK looks for its settings. Real environment variables take
// precedence, and a missing .env is fine (e.g. on Cloud Run). Values still set
// to an unfilled .env.example placeholder are treated as unset.
func loadEnv() {
	_ = godotenv.Load()
	for _, kv := range os.Environ() {
		key, value, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(value, "<TODO") {
			_ = os.Unsetenv(key)
		}
	}
}

// lazyModel is a Gemini model that creates its client on first use. Creating a
// Vertex AI client looks up credentials, so doing it at startup would stop the
// server from starting at all when credentials are missing; deferring it turns
// that into an error on the first request instead.
type lazyModel struct {
	name string
	llm  func() (model.LLM, error)
}

func newLazyModel(ctx context.Context, name string) *lazyModel {
	return &lazyModel{
		name: name,
		llm: sync.OnceValues(func() (model.LLM, error) {
			// An empty config makes the SDK read GOOGLE_GENAI_USE_VERTEXAI,
			// GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION and GOOGLE_API_KEY.
			return gemini.NewModel(ctx, name, &genai.ClientConfig{})
		}),
	}
}

func (m *lazyModel) Name() string {
	return m.name
}

func (m *lazyModel) GenerateContent(
	ctx context.Context, req *model.LLMRequest, stream bool,
) iter.Seq2[*model.LLMResponse, error] {
	llm, err := m.llm()
	if err != nil {
		return func(yield func(*model.LLMResponse, error) bool) {
			yield(nil, err)
		}
	}
	return llm.GenerateContent(ctx, req, stream)
}
