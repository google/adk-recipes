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

"""Tools for the in_trip, trip_monitor and day_of agents."""

from datetime import datetime
from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext

from travel_concierge.shared_libraries import constants
from travel_concierge.sub_agents.in_trip import prompt


def flight_status_check(
    flight_number: str, flight_date: str, checkin_time: str, departure_time: str
):
    """Checks the status of a flight, given its flight_number, date, checkin_time and departure_time."""
    print("Checking", flight_number, flight_date, checkin_time, departure_time)
    return {"status": f"Flight {flight_number} checked"}


def event_booking_check(event_name: str, event_date: str, event_location: str):
    """Checks the status of an event that requires booking, given its event_name, date, and event_location."""
    print("Checking", event_name, event_date, event_location)
    if event_name.startswith(
        "Space Needle"
    ):  # Mocking an exception to illustrate
        return {"status": f"{event_name} is closed."}
    return {"status": f"{event_name} checked"}


def weather_impact_check(
    activity_name: str, activity_date: str, activity_location: str
):
    """
    Checks the status of an outdoor activity that may be impacted by weather, given its name, date, and its location.

    Args:
        activity_name: The name of the activity.
        activity_date: The date of the activity.
        activity_location: The location of the activity.

    Returns:
        A dictionary containing the status of the activity.
    """
    print("Checking", activity_name, activity_date, activity_location)
    return {"status": f"{activity_name} checked"}


def get_event_time_as_destination(
    destin_json: dict[str, Any], default_value: str
):
    """Returns an event time appropriate for the location type."""
    match destin_json["event_type"]:
        case "flight":
            return destin_json["boarding_time"]
        case "hotel":
            return destin_json["check_in_time"]
        case "visit":
            return destin_json["start_time"]
        case _:
            return default_value


def parse_as_origin(origin_json: dict[str, Any]):
    """Returns a tuple of strings (origin, depart_by) appropriate for the starting location."""
    match origin_json["event_type"]:
        case "flight":
            return (
                origin_json["arrival_airport"] + " Airport",
                origin_json["arrival_time"],
            )
        case "hotel":
            return (
                origin_json["description"]
                + " "
                + origin_json.get("address", ""),
                "any time",
            )
        case "visit":
            return (
                origin_json["description"]
                + " "
                + origin_json.get("address", ""),
                origin_json["end_time"],
            )
        case "home":
            return (
                origin_json.get("local_prefer_mode")
                + " from "
                + origin_json.get("address", ""),
                "any time",
            )
        case _:
            return "Local in the region", "any time"


def parse_as_destin(destin_json: dict[str, Any]):
    """Returns a tuple of strings (destination, arrive_by) appropriate for the destination."""
    match destin_json["event_type"]:
        case "flight":
            return (
                destin_json["departure_airport"] + " Airport",
                "An hour before " + destin_json["boarding_time"],
            )
        case "hotel":
            return (
                destin_json["description"]
                + " "
                + destin_json.get("address", ""),
                "any time",
            )
        case "visit":
            return (
                destin_json["description"]
                + " "
                + destin_json.get("address", ""),
                destin_json["start_time"],
            )
        case "home":
            return (
                destin_json.get("local_prefer_mode")
                + " to "
                + destin_json.get("address", ""),
                "any time",
            )
        case _:
            return "Local in the region", "as soon as possible"


def find_segment(
    profile: dict[str, Any], itinerary: dict[str, Any], current_datetime: str
):
    """
    Find the events to travel from A to B
    This follows the itinerary schema in types.Itinerary.

    Since return values will be used as part of the prompt,
    there are flexibilities in what the return values contains.

    Args:
        profile: A dictionary containing the user's profile.
        itinerary: A dictionary containing the user's itinerary.
        current_datetime: A string containing the current date and time.

    Returns:
      from - capture information about the origin of this segment.
      to   - capture information about the destination of this segment.
      arrive_by - an indication of the time we shall arrive at the destination.
    """
    if not current_datetime:
        current_datetime = "2025-06-15 00:00:00"
    try:
        datetime_object = datetime.fromisoformat(current_datetime)
    except ValueError:
        datetime_object = datetime.now()
    current_date = datetime_object.strftime("%Y-%m-%d")
    current_time = datetime_object.strftime("%H:%M")
    event_date = current_date
    event_time = current_time

    print("-----")
    print("MATCH DATE", current_date, current_time)
    print("-----")

    # defaults
    origin_json = profile.get("home", {})
    destin_json = profile.get("home", {})

    leave_by = "No movement required"
    arrive_by = "No movement required"

    # Go through the itinerary to find where we are base on the current date and time
    for day in itinerary.get("days", []):
        event_date = day["date"]
        for event in day["events"]:
            # for every event we update the origin and destination until
            # we find one we need to pay attention
            origin_json = destin_json
            destin_json = event
            event_time = get_event_time_as_destination(
                destin_json, current_time
            )
            # The moment we find an event that's in the immediate future we stop to handle it
            print(
                event["event_type"],
                event_date,
                current_date,
                event_time,
                current_time,
            )
            if event_date >= current_date and event_time >= current_time:
                break
        else:  # if inner loop not break, continue
            continue
        break  # else break too.

    #
    # Construct prompt descriptions for travel_from, travel_to, arrive_by
    #
    travel_from, leave_by = parse_as_origin(origin_json)
    travel_to, arrive_by = parse_as_destin(destin_json)

    return (travel_from, travel_to, leave_by, arrive_by)


def _inspect_itinerary(state: dict[str:Any]):
    """Identifies and returns the itinerary, profile and current datetime from the session state."""

    itinerary = state.get(constants.ITIN_KEY, {})
    profile = state.get(constants.PROF_KEY, {})
    print("itinerary", itinerary)
    current_datetime = (
        itinerary.get("start_date", "") + " 00:00"
        if itinerary.get("start_date")
        else ""
    )
    if state.get(constants.ITIN_DATETIME, ""):
        current_datetime = state[constants.ITIN_DATETIME]
    if not current_datetime:
        current_datetime = "2025-06-15 00:00:00"

    return itinerary, profile, current_datetime


def transit_coordination(readonly_context: ReadonlyContext):
    """Dynamically generates an instruction for the day_of agent."""

    state = readonly_context.state

    # Inspecting the itinerary
    if constants.ITIN_KEY not in state:
        return prompt.NEED_ITIN_INSTR

    itinerary, profile, current_datetime = _inspect_itinerary(state)
    travel_from, travel_to, leave_by, arrive_by = find_segment(
        profile, itinerary, current_datetime
    )

    print("-----")
    print(itinerary.get("trip_name", ""))
    print(current_datetime)
    print("-----")
    print("-----")
    print("TRIP EVENT")
    print("FROM", travel_from, leave_by)
    print("TO", travel_to, arrive_by)
    print("-----")

    return prompt.LOGISTIC_INSTR_TEMPLATE.format(
        CURRENT_TIME=current_datetime,
        TRAVEL_FROM=travel_from,
        LEAVE_BY_TIME=leave_by,
        TRAVEL_TO=travel_to,
        ARRIVE_BY_TIME=arrive_by,
    )
