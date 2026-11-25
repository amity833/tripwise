from .definitions import (
    check_policy_tool,
    list_bookings_tool,
    propose_booking_tool,
    resolve_date_tool,
    search_flights_tool,
    search_hotels_tool,
)
from .registry import ToolRegistry


def default_tools() -> ToolRegistry:
    return ToolRegistry([search_flights_tool, search_hotels_tool, check_policy_tool, propose_booking_tool, list_bookings_tool, resolve_date_tool])
