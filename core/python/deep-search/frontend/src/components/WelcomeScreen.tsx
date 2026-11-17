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

import { Button } from "@/components/ui/button";
import { InputForm } from "@/components/InputForm";

interface WelcomeScreenProps {
  handleSubmit: (query: string) => void;
  isLoading: boolean;
  onCancel: () => void;
}

export function WelcomeScreen({
  handleSubmit,
  isLoading,
  onCancel,
}: WelcomeScreenProps) {
  return (
    // This container fills the space provided by its parent layout (e.g., the left panel in a split view)
    // and centers its content (the card) within itself.
    <div className="flex-1 flex flex-col items-center justify-center p-4 overflow-hidden relative">
      {/* The "Card" Container */}
      {/* This div now holds the card's styling: background, blur, padding, border, shadow, and hover effect */}
      <div
        className="w-full max-w-2xl z-10
                      bg-neutral-900/50 backdrop-blur-md 
                      p-8 rounded-2xl border border-neutral-700 
                      shadow-2xl shadow-black/60 
                      transition-all duration-300 hover:border-neutral-600"
      >
        {/* Header section of the card */}
        <div className="text-center space-y-4">
          <h1 className="text-4xl font-bold text-white flex items-center justify-center gap-3">
            ✨ Deep Search - ADK 🚀
          </h1>
          <p className="text-lg text-neutral-300 max-w-md mx-auto">
            Turns your questions into comprehensive reports!
          </p>
        </div>

        {/* Input form section of the card */}
        <div className="mt-8">
          <InputForm
            onSubmit={handleSubmit}
            isLoading={isLoading}
            context="homepage"
          />
          {isLoading && (
            <div className="mt-4 flex justify-center">
              <Button
                variant="outline"
                onClick={onCancel}
                className="text-red-400 hover:text-red-300 hover:bg-red-900/20 border-red-700/50" // Enhanced cancel button
              >
                Cancel
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
