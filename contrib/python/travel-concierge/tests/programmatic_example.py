# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This is a rudimentary example showing how to interact with an ADK agent as a server end point."""

import json

import requests

#
# This client connects to an existing end point created by running `adk api_server <agent package>`
# This client also illustrates how one can use the adk events streamed from the server side to inform user interface components.
#

# Endpoint created by running `adk api_server travel_concierge``
RUN_ENDPOINT = "http://127.0.0.1:8000/run_sse"
HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "text/event-stream",
}

# Create a session if it doesn't exist
SESSION_ENDPOINT = "http://127.0.0.1:8000/apps/travel_concierge/users/traveler0115/sessions/session_2449"
response = requests.post(SESSION_ENDPOINT)
print("Session", response.json())

# We are going to run just two turns with the concierge
user_inputs = [
    "Inspire me about the Maldives",
    "Show me a few activites around Baa Atoll",
]

for user_input in user_inputs:
    DATA = {
        "session_id": "session_2449",
        "app_name": "travel_concierge",
        "user_id": "traveler0115",
        "new_message": {
            "role": "user",
            "parts": [
                {
                    "text": user_input,
                }
            ],
        },
    }

    print(f'\n[user]: "{user_input}"')

    with requests.post(
        RUN_ENDPOINT, data=json.dumps(DATA), headers=HEADERS, stream=True
    ) as r:
        for chunk in r.iter_lines():  # or, for line in r.iter_lines():
            # These events and its content can be inspected and leveraged.
            # This under-pins application integration;
            if not chunk:
                continue
            json_string = chunk.decode("utf-8").removeprefix("data: ").strip()
            event = json.loads(json_string)

            # {'error': 'Function activities_agent is not found in the tools_dict.'}
            if "content" not in event:
                print(event)
                continue

            author = event["author"]
            # Uncomment this to see the full event payload
            # print(f"\n[{author}]: {json.dumps(event)}")
            # continue

            function_calls = [
                e["functionCall"]
                for e in event["content"]["parts"]
                if "functionCall" in e
            ]
            function_responses = [
                e["functionResponse"]
                for e in event["content"]["parts"]
                if "functionResponse" in e
            ]

            if "text" in event["content"]["parts"][0]:
                text_response = event["content"]["parts"][0]["text"]
                print(f"\n{author} {text_response}")

            if function_calls:
                for function_call in function_calls:
                    name = function_call["name"]
                    args = function_call["args"]
                    print(
                        f'\n{author}\nfunction call: "{name}"\nargs: {json.dumps(args, indent=2)}\n'
                    )

            elif function_responses:
                for function_response in function_responses:
                    function_name = function_response["name"]
                    application_payload = json.dumps(
                        function_response["response"], indent=2
                    )
                    print(
                        f'\n{author}\nResponse from: "{name}"\nresponse: {application_payload}\n'
                    )

                    # A switch case statement against the function_name allows
                    # an application to act according to which agent / tool the response originated from.
                    match function_name:
                        case "place_agent":
                            print(
                                "\n[app]: To render a carousel of destinations"
                            )
                        case "map_tool":
                            print("\n[app]: To render a map of pois")
                        case "flight_selection_agent":
                            print("\n[app]: Render a list")
                        case "hotel_selection_agent":
                            print("\n[app]: Render a list")
                    # ... etc
