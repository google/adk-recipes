# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


def evaluate(instance):
    prompt_text = ""
    if "prompt" in instance:
        for part in instance["prompt"].get("parts", []):
            if "text" in part:
                prompt_text += part["text"].lower()

    is_acc123 = "acc-123" in prompt_text

    if is_acc123:
        offered = False
        for turn in instance.get("agent_data", {}).get("turns", []):
            for event in turn.get("events", []):
                content = event.get("content", {})
                if content.get("role") == "model":
                    for part in content.get("parts", []):
                        if (
                            "function_call" in part
                            and part["function_call"].get("name")
                            == "offer_retention"
                        ):
                            offered = True
        if offered:
            return {
                "score": 0,
                "explanation": "Retention improperly offered to already discounted account.",
            }
        else:
            return {"score": 1, "explanation": "Retention correctly withheld."}

    return {"score": 1, "explanation": "N/A"}
