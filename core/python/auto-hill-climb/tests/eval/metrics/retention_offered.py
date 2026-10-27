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
    # Extract the original prompt from instance['prompt']
    prompt_text = ""
    if "prompt" in instance:
        for part in instance["prompt"].get("parts", []):
            if "text" in part:
                prompt_text += part["text"].lower()

    wants_cancel = "cancel" in prompt_text or "end" in prompt_text
    is_acc123 = "acc-123" in prompt_text
    already_declined = (
        "already declined" in prompt_text or "already rejected" in prompt_text
    )

    if wants_cancel and not is_acc123 and not already_declined:
        offered = False
        for turn in instance.get("agent_data", {}).get("turns", []):
            for event in turn.get("events", []):
                content = event.get("content", {})
                if content.get("role") == "model":
                    for part in content.get("parts", []):
                        if (
                            "function_call" in part
                            and part["function_call"].get("name") == "offer_retention"
                        ):
                            offered = True
        if offered:
            return {"score": 1, "explanation": "Retention offered."}
        else:
            return {
                "score": 0,
                "explanation": "Retention not offered for eligible account.",
            }

    return {"score": 1, "explanation": "N/A"}
