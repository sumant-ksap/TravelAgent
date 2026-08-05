import ast
import asyncio
import datetime
import logging
import operator
import os
import time

import httpx

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {"User-Agent": "TravelAgentBot/1.0 (+telegram travel planning assistant)"}

# The main public Overpass instance is a shared free server that occasionally
# returns 504 under load. In production testing, the commonly-recommended
# mirrors (kumi.systems, openstreetmap.ru) were consistently unreachable from
# this network (timeouts / connection failures on every attempt) rather than
# offering real redundancy, so instead of trusting a fallback list, we retry
# the primary a few times - a transient 504 usually clears within a few seconds.
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_OVERPASS_ATTEMPTS = 3
_OVERPASS_RETRY_DELAY_SECONDS = 2.0

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow fall",
    73: "moderate snow fall",
    75: "heavy snow fall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

_PLACE_CATEGORIES = {
    "attraction": '["tourism"~"attraction|museum|viewpoint|gallery|zoo|theme_park"]',
    "restaurant": '["amenity"="restaurant"]',
    "cafe": '["amenity"~"cafe|fast_food"]',
    "park": '["leisure"~"park|garden"]',
    "shopping": '["shop"~"mall|department_store"]',
    "nightlife": '["amenity"~"bar|pub|nightclub"]',
}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Unsupported expression")


async def _geocode(client: httpx.AsyncClient, location: str):
    resp = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1},
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    place = results[0]
    label = ", ".join(
        part for part in [place.get("name"), place.get("admin1"), place.get("country")] if part
    )
    return place["latitude"], place["longitude"], label


# ---------------------------------------------------------------------------
# Optional Amadeus flight/hotel integration (activates only if API keys are set)
# ---------------------------------------------------------------------------

_amadeus_token = {"value": None, "expires_at": 0.0}


async def _amadeus_token_get(client: httpx.AsyncClient, base: str, key: str, secret: str):
    now = time.monotonic()
    if _amadeus_token["value"] and now < _amadeus_token["expires_at"]:
        return _amadeus_token["value"]
    resp = await client.post(
        f"{base}/v1/security/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": key, "client_secret": secret},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    data = resp.json()
    _amadeus_token["value"] = data["access_token"]
    _amadeus_token["expires_at"] = now + float(data.get("expires_in", 1800)) - 30
    return _amadeus_token["value"]


async def _amadeus_resolve_location(client, token, base, keyword, subtype):
    if len(keyword) == 3 and keyword.isalpha() and keyword.isupper():
        return keyword
    resp = await client.get(
        f"{base}/v1/reference-data/locations",
        params={"keyword": keyword, "subType": subtype, "page[limit]": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        return None
    return data[0].get("iataCode")


# ---------------------------------------------------------------------------
# Tool implementations. Every handler has the signature (arguments, *, memory, chat_id)
# ---------------------------------------------------------------------------


async def calculator(arguments, *, memory=None, chat_id=None) -> str:
    expression = arguments.get("expression", "")
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
        return str(result)
    except Exception as exc:
        return f"Error evaluating expression: {exc}"


async def current_datetime(arguments, *, memory=None, chat_id=None) -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def web_search(arguments, *, memory=None, chat_id=None) -> str:
    query = arguments.get("query", "")
    if not query:
        return "No query provided."
    async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=15.0) as client:
        response = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        )
    response.raise_for_status()
    data = response.json()
    summary = data.get("AbstractText") or ""
    if not summary:
        topics = data.get("RelatedTopics") or []
        texts = [t.get("Text") for t in topics if isinstance(t, dict) and t.get("Text")]
        summary = " | ".join(texts[:3])
    return summary or "No results found."


async def get_weather(arguments, *, memory=None, chat_id=None) -> str:
    location = arguments.get("location", "")
    if not location:
        return "No location provided."
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=15.0) as client:
            geo = await _geocode(client, location)
            if not geo:
                return f"Could not find location: {location}"
            lat, lon, label = geo
            weather_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon, "current_weather": "true"},
            )
        weather_response.raise_for_status()
        weather_data = weather_response.json()
    except Exception as exc:
        return f"Weather lookup failed: {exc}"

    current = weather_data.get("current_weather") or {}
    if not current:
        return f"No current weather data available for {label}."

    code = current.get("weathercode")
    condition = _WEATHER_CODES.get(code, f"weather code {code}")
    return (
        f"Current weather in {label}: {current.get('temperature')}°C, {condition}, "
        f"wind {current.get('windspeed')} km/h."
    )


async def search_memory(arguments, *, memory=None, chat_id=None) -> str:
    keyword = arguments.get("keyword", "")
    limit = arguments.get("limit", 5)
    if memory is None or chat_id is None:
        return "Memory search is unavailable."
    if not keyword:
        return "No keyword provided to search for."

    matches = await memory.search(chat_id, keyword, limit or 5)
    if not matches:
        return f"No past messages found containing '{keyword}'."

    lines = [f"[{m['created_at']}] {m['role']}: {m['content']}" for m in matches]
    return "\n".join(lines)


async def currency_convert(arguments, *, memory=None, chat_id=None) -> str:
    amount = arguments.get("amount")
    from_currency = (arguments.get("from_currency") or "").upper()
    to_currency = (arguments.get("to_currency") or "").upper()
    if amount is None or not from_currency or not to_currency:
        return "Missing required arguments: amount, from_currency, to_currency."
    if from_currency == to_currency:
        return f"{amount} {from_currency} = {amount} {to_currency}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=15.0) as client:
            resp = await client.get(
                "https://api.frankfurter.dev/v1/latest",
                params={"amount": amount, "from": from_currency, "to": to_currency},
            )
        resp.raise_for_status()
        data = resp.json()
        rate_amount = (data.get("rates") or {}).get(to_currency)
        if rate_amount is None:
            return f"Could not get an exchange rate from {from_currency} to {to_currency}."
        return f"{amount} {from_currency} = {rate_amount:.2f} {to_currency} (ECB reference rate, {data.get('date')})"
    except Exception as exc:
        return f"Currency conversion failed: {exc}"


async def search_places(arguments, *, memory=None, chat_id=None) -> str:
    location = arguments.get("location", "")
    category = (arguments.get("category") or "attraction").lower()
    radius_km = float(arguments.get("radius_km") or 5)
    limit = int(arguments.get("limit") or 8)
    if not location:
        return "Missing required argument: location."

    tag_filter = _PLACE_CATEGORIES.get(category, _PLACE_CATEGORIES["attraction"])

    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=20.0) as client:
            geo = await _geocode(client, location)
            if not geo:
                return f"Could not find location: {location}"
            lat, lon, label = geo
            radius_m = int(radius_km * 1000)
            query = (
                "[out:json][timeout:20];"
                f'(node{tag_filter}(around:{radius_m},{lat},{lon});'
                f'way{tag_filter}(around:{radius_m},{lat},{lon}););'
                f"out center {limit};"
            )

            elements = None
            last_error = None
            for attempt in range(_OVERPASS_ATTEMPTS):
                try:
                    resp = await client.post(_OVERPASS_URL, data={"data": query})
                    resp.raise_for_status()
                    elements = resp.json().get("elements") or []
                    break
                except Exception as exc:  # noqa: BLE001 - retry a transient overload
                    last_error = exc
                    if attempt < _OVERPASS_ATTEMPTS - 1:
                        await asyncio.sleep(_OVERPASS_RETRY_DELAY_SECONDS)
            if elements is None:
                raise last_error or RuntimeError("Overpass request failed")
    except Exception as exc:
        return f"Place search failed: {exc}"

    if not elements:
        return f"No {category} results found near {label}."

    lines = []
    for el in elements[:limit]:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        extra = tags.get("cuisine") or tags.get("tourism") or tags.get("amenity") or ""
        lines.append(f"- {name}" + (f" ({extra})" if extra else ""))

    if not lines:
        return f"Found {len(elements)} {category} result(s) near {label} but none had usable names."
    return f"{category.title()} options near {label} (OpenStreetMap data):\n" + "\n".join(lines)


async def get_route(arguments, *, memory=None, chat_id=None) -> str:
    origin = arguments.get("origin", "")
    destination = arguments.get("destination", "")
    if not origin or not destination:
        return "Missing required arguments: origin, destination."

    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=20.0) as client:
            origin_geo = await _geocode(client, origin)
            dest_geo = await _geocode(client, destination)
            if not origin_geo or not dest_geo:
                return f"Could not geocode '{origin}' or '{destination}'."
            olat, olon, olabel = origin_geo
            dlat, dlon, dlabel = dest_geo
            resp = await client.get(
                f"https://router.project-osrm.org/route/v1/driving/{olon},{olat};{dlon},{dlat}",
                params={"overview": "false"},
            )
        resp.raise_for_status()
        routes = resp.json().get("routes") or []
    except Exception as exc:
        return f"Route lookup failed: {exc}"

    if not routes:
        return f"No road route found between {olabel} and {dlabel}."

    route = routes[0]
    distance_km = route["distance"] / 1000
    duration_min = route["duration"] / 60
    return (
        f"{olabel} -> {dlabel}: approx {distance_km:.1f} km, {duration_min:.0f} min by road "
        "(OSRM driving-network estimate; actual public-transit/walking/flight times may differ)."
    )


async def search_flights(arguments, *, memory=None, chat_id=None) -> str:
    origin = arguments.get("origin", "")
    destination = arguments.get("destination", "")
    departure_date = arguments.get("departure_date", "")
    return_date = arguments.get("return_date")
    adults = int(arguments.get("adults") or 1)
    currency = arguments.get("currency") or "USD"

    if not origin or not destination or not departure_date:
        return "Missing required arguments: origin, destination, departure_date."

    key = os.getenv("AMADEUS_API_KEY")
    secret = os.getenv("AMADEUS_API_SECRET")
    if not key or not secret:
        return (
            "No live flight-fare provider is configured (AMADEUS_API_KEY/AMADEUS_API_SECRET not set). "
            "Do not invent fares or schedules. Use web_search to research typical routes/airlines for "
            "this trip and clearly label anything you report as an unverified estimate, not a live fare."
        )

    base = os.getenv("AMADEUS_API_HOST", "https://test.api.amadeus.com")
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=20.0) as client:
            token = await _amadeus_token_get(client, base, key, secret)
            origin_code = await _amadeus_resolve_location(client, token, base, origin, "AIRPORT")
            dest_code = await _amadeus_resolve_location(client, token, base, destination, "AIRPORT")
            if not origin_code or not dest_code:
                return f"Could not resolve airport codes for '{origin}' / '{destination}'."

            params = {
                "originLocationCode": origin_code,
                "destinationLocationCode": dest_code,
                "departureDate": departure_date,
                "adults": adults,
                "max": 5,
                "currencyCode": currency,
            }
            if return_date:
                params["returnDate"] = return_date

            resp = await client.get(
                f"{base}/v2/shopping/flight-offers",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        offers = resp.json().get("data") or []
    except httpx.HTTPStatusError as exc:
        logger.warning("Amadeus flight search failed: %s", exc)
        return f"Flight provider returned an error ({exc.response.status_code}). Report this instead of guessing fares."
    except Exception as exc:
        logger.warning("Amadeus flight search failed: %s", exc)
        return f"Flight search failed: {exc}. Report this instead of guessing fares."

    if not offers:
        return f"No live flight offers found for {origin_code}->{dest_code} on {departure_date}."

    lines = []
    for offer in offers[:5]:
        price = offer.get("price", {})
        seg_descs = []
        for itin in offer.get("itineraries", []):
            segments = itin.get("segments", [])
            if not segments:
                continue
            first, last = segments[0], segments[-1]
            seg_descs.append(
                f"{first['departure']['iataCode']} {first['departure'].get('at', '')} -> "
                f"{last['arrival']['iataCode']} {last['arrival'].get('at', '')} "
                f"({len(segments) - 1} stop(s), {itin.get('duration', '?')})"
            )
        lines.append(f"- {price.get('total')} {price.get('currency')} | " + " | ".join(seg_descs))

    return "Live flight offers (Amadeus test data):\n" + "\n".join(lines)


async def search_hotels(arguments, *, memory=None, chat_id=None) -> str:
    city = arguments.get("city", "")
    check_in = arguments.get("check_in", "")
    check_out = arguments.get("check_out", "")
    adults = int(arguments.get("adults") or 1)

    if not city or not check_in or not check_out:
        return "Missing required arguments: city, check_in, check_out."

    key = os.getenv("AMADEUS_API_KEY")
    secret = os.getenv("AMADEUS_API_SECRET")
    if not key or not secret:
        return (
            "No live hotel-rate provider is configured (AMADEUS_API_KEY/AMADEUS_API_SECRET not set). "
            "Do not invent prices or availability. Use web_search to research neighborhoods and typical "
            "hotel categories for this city and clearly label anything you report as an unverified estimate."
        )

    base = os.getenv("AMADEUS_API_HOST", "https://test.api.amadeus.com")
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_HTTP_HEADERS, timeout=20.0) as client:
            token = await _amadeus_token_get(client, base, key, secret)
            city_code = await _amadeus_resolve_location(client, token, base, city, "CITY")
            if not city_code:
                return f"Could not resolve a city code for '{city}'."

            hotels_resp = await client.get(
                f"{base}/v1/reference-data/locations/hotels/by-city",
                params={"cityCode": city_code},
                headers={"Authorization": f"Bearer {token}"},
            )
            hotels_resp.raise_for_status()
            hotel_ids = [
                h["hotelId"] for h in (hotels_resp.json().get("data") or [])[:20] if h.get("hotelId")
            ]
            if not hotel_ids:
                return f"No hotels found in Amadeus reference data for {city_code}."

            offers_resp = await client.get(
                f"{base}/v3/shopping/hotel-offers",
                params={
                    "hotelIds": ",".join(hotel_ids),
                    "checkInDate": check_in,
                    "checkOutDate": check_out,
                    "adults": adults,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        offers_resp.raise_for_status()
        offers = offers_resp.json().get("data") or []
    except httpx.HTTPStatusError as exc:
        logger.warning("Amadeus hotel search failed: %s", exc)
        return f"Hotel provider returned an error ({exc.response.status_code}). Report this instead of guessing rates."
    except Exception as exc:
        logger.warning("Amadeus hotel search failed: %s", exc)
        return f"Hotel search failed: {exc}. Report this instead of guessing rates."

    if not offers:
        return f"No live hotel offers found in {city_code} for {check_in} to {check_out}."

    lines = []
    for entry in offers[:8]:
        hotel = entry.get("hotel", {})
        hotel_offers = entry.get("offers", [])
        if not hotel_offers:
            continue
        offer = hotel_offers[0]
        price = offer.get("price", {})
        lines.append(
            f"- {hotel.get('name', 'Unknown hotel')}: {price.get('total')} {price.get('currency')} total "
            f"({check_in} to {check_out})"
        )

    if not lines:
        return f"Hotels found in {city_code} but no bookable offers for these dates."
    return "Live hotel offers (Amadeus test data):\n" + "\n".join(lines)


TOOL_HANDLERS = {
    "calculator": calculator,
    "current_datetime": current_datetime,
    "web_search": web_search,
    "get_weather": get_weather,
    "search_memory": search_memory,
    "currency_convert": currency_convert,
    "search_places": search_places,
    "get_route": get_route,
    "search_flights": search_flights,
    "search_hotels": search_hotels,
}

ALL_TOOL_SCHEMAS = {
    "calculator": {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a basic arithmetic expression (+, -, *, /, **, parentheses) and return the numeric result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The arithmetic expression to evaluate, e.g. '(3 + 4) * 2'.",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    "current_datetime": {
        "type": "function",
        "function": {
            "name": "current_datetime",
            "description": "Get the current date and time (UTC).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information via DuckDuckGo and return a short summary.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    },
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a named location (city, optionally with region/country).",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Place name to look up, e.g. 'Bengaluru' or 'Paris, France'.",
                    }
                },
                "required": ["location"],
            },
        },
    },
    "search_memory": {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search this chat's past conversation history for messages containing a keyword or "
                "phrase. Use this when the user references something said earlier that is not in the "
                "currently visible history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Word or phrase to search for in past messages."},
                    "limit": {
                        "type": "integer",
                        "description": "Max number of matching messages to return (default 5).",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    "currency_convert": {
        "type": "function",
        "function": {
            "name": "currency_convert",
            "description": "Convert an amount from one currency to another using current ECB reference rates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Amount to convert."},
                    "from_currency": {"type": "string", "description": "3-letter source currency code, e.g. USD."},
                    "to_currency": {"type": "string", "description": "3-letter target currency code, e.g. EUR."},
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        },
    },
    "search_places": {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": (
                "Find real points of interest (from OpenStreetMap) near a location, filtered by category: "
                "attraction, restaurant, cafe, park, shopping, or nightlife."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Place name to search near, e.g. 'Rome, Italy'."},
                    "category": {
                        "type": "string",
                        "enum": list(_PLACE_CATEGORIES.keys()),
                        "description": "Type of place to search for. Defaults to 'attraction'.",
                    },
                    "radius_km": {"type": "number", "description": "Search radius in kilometers (default 5)."},
                    "limit": {"type": "integer", "description": "Max results to return (default 8)."},
                },
                "required": ["location"],
            },
        },
    },
    "get_route": {
        "type": "function",
        "function": {
            "name": "get_route",
            "description": (
                "Estimate road travel distance and duration between two named locations using OSRM. "
                "Driving-network estimate only; does not model walking/transit/flight times precisely."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Starting location name."},
                    "destination": {"type": "string", "description": "Destination location name."},
                },
                "required": ["origin", "destination"],
            },
        },
    },
    "search_flights": {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": (
                "Search live flight offers between two airports/cities on a given date. Returns a clear "
                "'not configured' message instead of fares if no flight provider API key is set - never "
                "treat that message as if it were pricing data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Origin city or airport name/IATA code."},
                    "destination": {"type": "string", "description": "Destination city or airport name/IATA code."},
                    "departure_date": {"type": "string", "description": "Departure date, YYYY-MM-DD."},
                    "return_date": {"type": "string", "description": "Optional return date, YYYY-MM-DD."},
                    "adults": {"type": "integer", "description": "Number of adult travelers (default 1)."},
                    "currency": {"type": "string", "description": "Preferred 3-letter currency code (default USD)."},
                },
                "required": ["origin", "destination", "departure_date"],
            },
        },
    },
    "search_hotels": {
        "type": "function",
        "function": {
            "name": "search_hotels",
            "description": (
                "Search live hotel offers in a city for given check-in/check-out dates. Returns a clear "
                "'not configured' message instead of rates if no hotel provider API key is set - never "
                "treat that message as if it were pricing data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name or IATA city code."},
                    "check_in": {"type": "string", "description": "Check-in date, YYYY-MM-DD."},
                    "check_out": {"type": "string", "description": "Check-out date, YYYY-MM-DD."},
                    "adults": {"type": "integer", "description": "Number of adult travelers (default 1)."},
                },
                "required": ["city", "check_in", "check_out"],
            },
        },
    },
}


def get_tool_schemas(names) -> list:
    return [ALL_TOOL_SCHEMAS[name] for name in names if name in ALL_TOOL_SCHEMAS]


async def call_tool(name: str, arguments: dict, *, memory=None, chat_id=None, allowed=None) -> str:
    if allowed is not None and name not in allowed:
        return f"Tool '{name}' is not permitted for this agent."
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return await handler(arguments, memory=memory, chat_id=chat_id)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"Tool '{name}' failed: {exc}"
