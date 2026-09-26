# Travel Concierge

This sample demonstrates the use of Agent Development Kit to deliver a new user experience for Travelers. A cohort of agents mimics the notion of having a personal travel concierge, taking care of a traveler's needs: from trip conception, planning and booking, to preparing for the trip, getting help to get from point A to B during the trip, while simultaneously acting as an informative guide.

This example includes illustrations with ADK supported tools such as Google Maps Grounding API, Google Search Grounding and MCP.

The [Google Agents CLI](https://github.com/google/agents-cli) is the **recommended** way to create a new project from this sample. The copy in [adk-samples](https://github.com/google/adk-samples) remains the upstream source for browsing and contributions.

## Overview

A traveler's experience can be divided into two stages: pre-booking and post-booking. In this example, each stage involves the use of multiple specialized agents working together to provide the concierge experience.

During the pre-booking stage, different agents are constructed to help the traveler with vacation inspirations, activities planning, finding flights and hotels, and helps with booking payment processing. The pre-booking stage ends with an itinerary for a trip.

In the post-booking stage, given a concrete itinerary, a different set of agents support the traveler's needs before, during and after the trip. For example, the pre-trip agent checks for visa and medical requirements, travel advisory, and storm status. The in-trip agent monitors for any changes to bookings, with a day-of agent that helps the traveler getting from A to B during the trip. The post-trip agent helps collect feedback and identify additional preferences for future travel plans.


## Agent Details
The key features of the Travel Concierge include:

| Feature | Description |
| --- | --- |
| **Interaction Type:** | Conversational |
| **Complexity:**  | Advanced |
| **Agent Type:**  | Multi Agent |
| **Components:**  | Tools, AgentTools, Memory |
| **Vertical:**  | Travel |

See section [MCP](#mcp) for an example using Airbnb's MCP search tool.

### Agent Architecture
Travel Concierge Agents Architecture

<img src="travel-concierge-arch.webp" alt="Travel Concierge's Multi-Agent Architecture" width="800"/>

### Component Details

Expand on the "Key Components" from above.
*   **Agents:**
    * `inspiration_agent` - Interacts with the user to make suggestions on destinations and activities, inspire the user to choose one.
    * `planning_agent` - Given a destination, start date, and duration, the planning agent helps the user select flights, seats and a hotel (mocked), then generate an itinerary containing the activities.
    * `booking_agent` - Given an itinerary, the booking agent will help process those items in the itinerary that requires payment.
    * `pre_trip_agent` - Intended to be invoked regularly before the trip starts; This agent fetches relevant trip information given its origin, destination, and the user's nationality.
    * `in_trip_agent`- Intended to be invoked frequently during the trip. This agent provide three services: monitor any changes in bookings (mocked), acts as an informative guide, and provides transit assistance.
    * `post_trip_agent` - In this example, the post trip agent asks the traveler about their experience and attempts to extract and store their various preferences based on the trip, so that the information could be useful in future interactions.
*   **Tools:**
    * `map_tool` - retrieves lat/long; geocoding an address with the Google Map API.
    * `memorize` - a function to memorize information from the dialog that are important to trip planning and to provide in-trip support.
*   **AgentTools:**  
    * `google_search_grounding` - used in the example for pre-trip information gather such as visa, medical, travel advisory...etc.
    * `what_to_pack` - suggests what to pack for the trip given the origin and destination.
    * `place_agent` - this recommends destinations.
    * `poi_agent` - this suggests activities given a destination.
    * `itinerary_agent` - called by the `planning_agent` to fully construct and represent an itinerary in JSON following a pydantic schema.
    * `day_of_agent` - called by the `in_trip_agent` to provide in_trip on the day and in the moment transit information, getting from A to B. Implemented using dynamic instructions.
    * `flight_search_agent` -  mocked flight search given origin, destination, outbound and return dates.
    * `flight_seat_selection_agent` -  mocked seat selection, some seats are not available.
    * `hotel_search_agent` - mocked hotel selection given destination, outbound and return dates.
    * `hotel_room_selection_agent` - mocked hotel room selection.
    * `confirm_reservation_agent` - mocked reservation.
    * `payment_choice` - mocked payment selection, Apple Pay will not succeed, Google Pay and Credit Card will.
    * `payment_agent` - mocked payment processing.
*   **Memory:** 
    * All agents and tools in this example use the Agent Development Kit's internal session state as memory.
    * The session state is used to store information such as the itinerary, and temporary AgentTools' responses.
    * There are a number of premade itineraries that can be loaded for test runs. See 'Running the Agent' below on how to run them.

## Prerequisites

- Python 3.11+
- Google Cloud project (for Vertex AI)
- An [API key](https://developers.google.com/maps/get-started#api-key) with access to the [Maps Grounding Lite API](https://developers.google.com/maps/ai/grounding-lite)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Google Agent Development Kit 1.0+

To enable access Maps Grounding Lite API:
- The API service "Maps Grounding Lite" must be enabled in the Google Cloud project.
- Make sure "Maps Grounding Lite API" is added to the list of APIs the API key can access.

### Recommended: Using Google Agents CLI

The Google Agents CLI is the recommended way to create and deploy a production-ready version of this agent.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this sample** (replace `my-travel-concierge` with your project name):

```bash
agents-cli create my-travel-concierge -a adk@travel-concierge
cd my-travel-concierge
```

Install dependencies (including dev tools for tests and evaluation):

```bash
uv sync --group dev
```

Configure the environment. At the project root, copy `.env.example` to `.env` and set variables. To use Vertex, enable the Vertex AI API in your project. Example:

```
# Choose Model Backend: 0 -> ML Dev, 1 -> Vertex
GOOGLE_GENAI_USE_VERTEXAI=1
# ML Dev backend config, when GOOGLE_GENAI_USE_VERTEXAI=0, ignore if using Vertex.
# GOOGLE_API_KEY=YOUR_VALUE_HERE

# Vertex backend config
GOOGLE_CLOUD_PROJECT=__YOUR_CLOUD_PROJECT_ID__
GOOGLE_CLOUD_LOCATION=us-east1

# Maps API
GOOGLE_MAPS_API_KEY=__YOUR_API_KEY_HERE__

# GCS Storage Bucket name - for Agent Engine deployment test
GOOGLE_CLOUD_STORAGE_BUCKET=YOUR_BUCKET_NAME_HERE

# Sample Scenario Path - Default is an empty itinerary
# This will be loaded upon first user interaction.
#
# Uncomment one of the two, or create your own.
#
# TRAVEL_CONCIERGE_SCENARIO=travel_concierge/profiles/itinerary_seattle_example.json
TRAVEL_CONCIERGE_SCENARIO=travel_concierge/profiles/itinerary_empty_default.json
```

Authenticate:

```bash
gcloud auth application-default login
```

During setup, the Google Agents CLI will prompt you for deployment options and adds production-oriented tooling (for example automated CI/CD deployment scripts).

<details>
<summary>Clone this repository directly (contributors and advanced use)</summary>

**New projects should still use the Google Agents CLI** as described above.

### Folder Structure

```
.
├── README.md
├── travel-concierge-arch.webp
├── pyproject.toml
├── travel_concierge/
│   ├── shared_libraries/
│   ├── tools/
│   └── sub_agents/
│       ├── inspiration/
│       ├── planning/
│       ├── booking/
│       ├── pre_trip/
│       ├── in_trip/
│       └── post_trip/
├── tests/
│   └── unit/
├── eval/
│   └── data/
└── deployment/
```

### Additional prerequisites (clone path)

- Install uv: [uv website](https://docs.astral.sh/uv/)

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/google/adk-samples.git
   cd adk-samples/python/agents/travel-concierge
   ```

   From here on, run commands under `travel-concierge/` unless stated otherwise.

2. Install dependencies:

   ```bash
   uv sync --group dev
   ```

3. Set up `.env` as in the recommended path (copy `.env.example` and fill in variables).

4. Authenticate:

   ```bash
   gcloud auth application-default login
   ```

</details>

## Running the Agent

### Using `adk`

ADK provides convenient ways to bring up agents locally and interact with them.
You may talk to the agent using the CLI:

```bash
uv run adk run travel_concierge
```

or via its web interface:
```bash
uv run adk web
```

This will start a local web server on your machine. You may open the URL, select "travel_concierge" in the top-left drop-down menu, and
a chatbot interface will appear on the right. 

The conversation is initially blank. For an outline on the concierge interaction, see the section [Sample Agent interaction](#sample-agent-interaction) 

Here is something to try: 
* "Need some destination ideas for the Americas"
* After interacting with the agents for a while, you may ask: "Go ahead to planning".


### Programmatic Access

Below is an example of interacting with the agent as a server using Python. 
Try it under the travel-concierge directory:

First, establish a quick development API server for the travel_concierge package.
```bash
adk api_server travel_concierge
```
This will start a fastapi server at http://127.0.0.1:8000.
You can access its API docs at http://127.0.0.1:8000/docs

Here is an example client that only call the server for two turns:
```bash
python tests/programmatic_example.py
```

You may notice that there are code to handle function responses. We will revisit this in the [GUI](#gui) section below.


### Sample Agent interaction

Two example sessions are provided to illustrate how the Travel Concierge operates.
- Trip planning from inspiration to finalized bookings for a trip to Peru ([`tests/pre_booking_sample.md`](tests/pre_booking_sample.md)).
- In-trip experience for a short get away to Seattle, simulating the passage of time using a tool ([`tests/post_booking_sample.md`](tests/post_booking_sample.md)).

### Worth Trying

Instead of interacting with the concierge one turn at time. Try giving it the entire instruction, including decision making criteria, and watch it work, e.g. 

  *"Find flights to London from JFK on April 20th for 4 days. Pick any flights and any seats; also Any hotels and room type. Make sure you pick seats for both flights. Go ahead and act on my behalf without my input, until you have selected everything, confirm with me before generating an itinerary."*

Without specifically optimizing for such usage, this cohort of agents seem to be able to operate by themselves on your behalf with very little input.


## Running Tests

To run the illustrative tests and evaluations, install the extra dependencies and run `pytest`:

```bash
uv sync --group dev
uv run pytest
```

The different tests can also be run separately:

To run the unit tests, just checking all agents and tools responds:
```bash
uv run pytest tests
```

To run agent trajectory tests:
```bash
uv run pytest eval
```

## Deploying the Agent

To deploy the agent to Agent Runtime, run the following command under `travel-concierge`:

```bash
uv sync --group deployment
uv run python deployment/deploy.py --create
```
When this command returns, if it succeeds it will print an Agent Runtime resource
id that looks something like this:
```
projects/************/locations/us-east1/reasoningEngines/7737333693403889664
```

To quickly test that the agent has successfully deployed,
run the following command for one turn with the agent "Looking for inspirations around the Americas":
```bash
uv run python deployment/deploy.py --quicktest --resource_id=<RESOURCE_ID>
```
This will return a stream of JSON payload indicating the deployed agent is functional.

To delete the agent, run the following command (using the resource ID returned previously):
```bash
uv run python deployment/deploy.py --delete --resource_id=<RESOURCE_ID>
```

## Application Development

### Callbacks and initial State

The `root_agent` in this demo currently has a `before_agent_callback` registered to load an initial state, such as user preferences and itinerary, from a file into the session state for interaction. The primary reason for this is to reduce the amount of set up necessary, and this makes it easy to use the ADK UIs.

In a realistic application scenario, initial states can be included when a new `Session` is being created, there by satisfying use cases where user preferences and other pieces of information are most likely loaded from external databases.
 
### Memory vs States

In this example, we are using the session states as memory for the concierge, to store the itinerary, and intermediate agent / tools / user preference responses. In a realistic application scenario, the source for user profiles should be an external database, and the 
reciprocal writes to session states from tools should in addition be persisted, as a write-through, to external databases dedicated for user profiles and itineraries. 

### MCP

An example using Airbnb's MCP server is included. ADK supports MCP and provides several MCP tools.
This example attaches the Airbnb search and listing MCP tools to the `planning_agent`, and ask the concierge to simply find an airbnb given certain dates. The concierge will transfer the request to the planning agent which in turn will call the Airbnb search MCP tool.

To try the example, first set up nodejs and npx from Node.js [website](https://nodejs.org/en/download)

Making sure:
```
$ which node
/Users/USERNAME/.nvm/versions/node/v22.14.0/bin/node

$ which npx
/Users/USERNAME/.nvm/versions/node/v22.14.0/bin/npx
```

Then, under the `travel-concierge/` directory, run the test with:
```
python -m tests.mcp_abnb
```

You will see outputs on the console similar to the following:
```
[user]: Find me an airbnb in San Diego, April 9th, to april 13th, no flights nor itinerary needed. No need to confirm, simply return 5 choicess, remember to include urls.

( Setting up the agent and the tool ) 

Server started with options: ignore-robots-txt
Airbnb MCP Server running on stdio

Inserting Airbnb MCP tools into Travel-Concierge...
...
FOUND planning_agent

( Execute: Runner.run_async() ) 

[root_agent]: transfer_to_agent( {"agent_name": "planning_agent"} )
...
[planning_agent]: airbnb_search( {"checkout": "2025-04-13", "location": "San Diego", "checkin": "2025-04-09"} )

[planning_agent]: airbnb_search responds -> {
  "searchUrl": "https://www.airbnb.com/s/San%20Diego/homes?checkin=2025-04-09&checkout=2025-04-13&adults=1&children=0&infants=0&pets=0",
  "searchResults": [
    {
      "url": "https://www.airbnb.com/rooms/24669593",
      "listing": {
        "id": "24669593",
        "title": "Room in San Diego",
        "coordinate": {
          "latitude": 32.82952,
          "longitude": -117.22201
        },
        "structuredContent": {
          "mapCategoryInfo": "Stay with Stacy, Hosting for 7 years"
        }
      },
      "avgRatingA11yLabel": "4.91 out of 5 average rating,  211 reviews",
      "listingParamOverrides": {
        "categoryTag": "Tag:8678",
        "photoId": "1626723618",
        "amenities": ""
      },
      "structuredDisplayPrice": {
        "primaryLine": {
          "accessibilityLabel": "$9,274 TWD for 4 nights"
        },
        "explanationData": {
          "title": "Price details",
          "priceDetails": "$2,319 TWD x 4 nights: $9,274 TWD"
        }
      }
    },
    ...
    ...
  ]
}

[planning_agent]: Here are 5 Airbnb options in San Diego for your trip from April 9th to April 13th, including the URLs:

1.  Room in San Diego: [https://www.airbnb.com/rooms/24669593](https://www.airbnb.com/rooms/24669593)
2.  Room in San Diego: [https://www.airbnb.com/rooms/5360158](https://www.airbnb.com/rooms/5360158)
3.  Room in San Diego: [https://www.airbnb.com/rooms/1374944285472373029](https://www.airbnb.com/rooms/1374944285472373029)
4.  Apartment in San Diego: [https://www.airbnb.com/rooms/808814447273523115](https://www.airbnb.com/rooms/808814447273523115)
5.  Room in San Diego: [https://www.airbnb.com/rooms/53010806](https://www.airbnb.com/rooms/53010806)
```

### GUI

A typical end-user will be interacting with agents via GUIs instead of pure text. The front-end concierge application will likely render several kinds of agent responses graphically and/or with rich media, for example:
- Destination ideas as a carousel of cards,
- Points of interest / Directions on a Map,
- Expandable videos, images, link outs.
- Selection of flights and hotels as lists with logos, 
- Selection of flight seats on a seating chart,
- Clickable templated responses.

Many of these can be achieved via ADK's Events. This is because:
- All function calls and function responses are reported as events by the session runner.
- In this travel-concierge example, several sub-agents and tools use an explicit pydantic schema and controlled generation to generate a JSON response. These agents are: place agent (for destinations), poi agent (for pois and activities), flights and hotels selection agents, seats and rooms selection agents, and itinerary.
- When a session runner service is wrapped as a server endpoint, the series of events carrying these JSON payloads can be streamed over to the application.
- When the application recognizes the payload schema by their source agent, it can therefore render the payload accordingly.

To see how to work with events, agents and tools responses, open the file [`tests/programmatic_example.py`](tests/programmatic_example.py).

Run the test client code with:
```
python tests/programmatic_example.py 
```

You will get outputs similar to this below:
```
[user]: "Inspire me about Maldives"

...

[root_agent]: transfer_to_agent( {"agent_name": "inspiration_agent"} )

...

[inspiration_agent]: place_agent responds -> {
  "id": "af-be786618-b60b-45ee-a801-c40fd6811e60",
  "name": "place_agent",
  "response": {
    "places": [
      {
        "name": "Malé",
        "country": "Maldives",
        "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1c/Male%2C_Maldives_panorama_2016.jpg/1280px-Male%2C_Maldives_panorama_2016.jpg",
        "highlights": "The vibrant capital city, offering bustling markets, historic mosques, and a glimpse into local Maldivian life.",
        "rating": "4.2"
      },
      {
        "name": "Baa Atoll",
        "country": "Maldives",
        "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/95/Baa_Atoll_Maldives.jpg/1280px-Baa_Atoll_Maldives.jpg",
        "highlights": "A UNESCO Biosphere Reserve, famed for its rich marine biodiversity, including manta rays and whale sharks, perfect for snorkeling and diving.",
        "rating": "4.8"
      },
      {
        "name": "Addu Atoll",
        "country": "Maldives",
        "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c3/Addu_Atoll_Maldives.jpg/1280px-Addu_Atoll_Maldives.jpg",
        "highlights": "The southernmost atoll, known for its unique equatorial vegetation, historic WWII sites, and excellent diving spots with diverse coral formations.",
        "rating": "4.5"
      }
    ]
  }
}

[app]: To render a carousel of destinations

[inspiration_agent]: Maldives is an amazing destination! I see three great options:

1.  **Malé:** The capital city, where you can experience local life, markets, and mosques.
2.  **Baa Atoll:** A UNESCO Biosphere Reserve, perfect for snorkeling and diving, with manta rays and whale sharks.
3.  **Addu Atoll:** The southernmost atoll, offering unique vegetation, WWII history, and diverse coral for diving.

Are any of these destinations sound interesting? I can provide you with some activities you can do in the destination you selected.

[user]: "Suggest some acitivities around Baa Atoll"

...

```

In an environment where the events are passed from the server running the agents to an application front-end, the application can use the method in this example to parse and identify which payload is being sent and choose the most appropriate payload renderer / handler.

## Customization

The following are some ideas how one can reuse the concierge and make it your own.

### Load a premade itinerary to demo the in-trip flow
- By default, a user profile and an empty itinerary is loaded from `travel_concierge/profiles/itinerary_empty_default.json`.
- To specify a different file to load, such as the Seattle example `travel_concierge/profiles/itinerary_seattle_example.json`:
  - Set the environmental variable `TRAVEL_CONCIERGE_SCENARIO` to `travel_concierge/profiles/itinerary_seattle_example.json` in the `.env`.
  - Then restart `adk web` and load the travel concierge.
- When you start interacting with the agent, the state will be loaded. 
- You can see the loaded user profile and itinerary when you select "State" in the GUI.


### Make your own premade itinerary for demos

- The Itinerary schema is defined in types.py
- Make a copy of `itinerary_seattle_example.json` and make your own `itinerary` following the schema.
- Use the above steps to load and test your new itinerary.
- For the `user_profile` dict:
  - `passport_nationality` and `home` are mandatory fields, modify only the `address` and `local_prefer_mode`.
  - You can modify / add additional profile fields to the 


### Integration with external APIs

There are many opportunities for enhancements, customizations and integration in this example:
- Connecting to real flights / seats selection systems
- Connecting to real hotels / rooms selection systems
- Usage of external memory persistence services, or databases, instead of the session's state
- Use of the Google Maps [Route API](https://developers.google.com/maps/documentation/routes) in `day_of` agent.
- Connect to external APIs for visa / medical / travel advisory and NOAA storm information instead of using Google Search Grounding.


### Refining Agents

The following are just the starting ideas:
- A more sophisticated itinerary and activity planning agent; For example, currently the agent does not handle flights with lay-over.
- Better accounting - accuracy in calculating costs on flights, hotels + others.
- A booking agent that is less mundane and more efficient
- For the pre-trip and in-trip agents, there are opportunities to dynamically adjusts the itinerary and resolves trip exceptions

## Troubleshoot

The following occasionally happens while interaction with the agent:
- "Malformed" function call or response, or pydantic errors - when this happens simply tell the agent to "try again". 
- If the agents tries to call a tool that doesn't exist, tell the agent that it is the "wrong tool, try again", the agent  is often able to self correct. 
- Similarly, if you have waited for a while and the agent has stopped in the middle of executing a series of actions, ask the agent "what's next" to nudge it forward.

These happens occasionally, it is likely due to variations in JSON responses that requires more rigorous experimentation on prompts and generation parameters to attain more stable results. Within an application, these retries can also be built into the application as part of exception handling.


## Disclaimer

This agent sample is provided for illustrative purposes only and is not intended for production use. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

This sample has not been rigorously tested, may contain bugs or limitations, and does not include features or optimizations typically required for a production environment (e.g., robust error handling, security measures, scalability, performance considerations, comprehensive logging, or advanced configuration options).

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this sample. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.