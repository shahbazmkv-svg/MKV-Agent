import requests
from datetime import date, timedelta

APPIC_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
APPIC_KEY = "96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0"


def fetch_appic_data(direction: str, start: str = None, end: str = None) -> list:
    """
    Fetch check-in or check-out records from Appic Fleet.
    direction: "Out" for checkouts, "In" for check-ins
    Defaults to today → +7 days if no dates given.
    """
    today     = date.today()
    start     = start or today.strftime("%Y-%m-%d")
    end       = end   or (today + timedelta(days=7)).strftime("%Y-%m-%d")

    resp = requests.post(
        APPIC_URL,
        data={
            "key":       APPIC_KEY,
            "startDate": start,
            "endDate":   end,
            "direction": direction,   # "Out" or "In"
        },
        timeout=10
    )
    resp.raise_for_status()
    result = resp.json()

    if not result.get("issuccess", True) is False:
        return result.get("data", result)  # return records
    return []


def get_availability_from_appic() -> dict:
    """
    Derive availability by comparing:
    - Vehicles checked OUT (currently rented, not available)
    - Vehicles checked IN today/upcoming (available soon)
    Returns dict: { "vehicle name (lowercase)": { available, returning_date } }
    """
    today = date.today().strftime("%Y-%m-%d")
    week  = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")

    checked_out = fetch_appic_data("Out", today, week)  # leaving fleet
    checked_in  = fetch_appic_data("In",  today, week)  # returning to fleet

    # Build a set of currently out vehicles
    out_names = {}
    for record in checked_out:
        name = (record.get("vehicle_name") or record.get("car") or "").lower().strip()
        ret  = record.get("return_date") or record.get("endDate") or "N/A"
        if name:
            out_names[name] = ret

    # Build set of vehicles returning soon
    returning = {}
    for record in checked_in:
        name = (record.get("vehicle_name") or record.get("car") or "").lower().strip()
        ret  = record.get("date") or record.get("startDate") or today
        if name:
            returning[name] = ret

    # Merge: out = unavailable unless returning today
    availability = {}
    for name, return_date in out_names.items():
        availability[name] = {
            "available":      False,
            "status":         "Checked out",
            "returning_date": return_date,
        }
    for name, checkin_date in returning.items():
        is_today = checkin_date == today
        availability[name] = {
            "available":      is_today,
            "status":         "Returning today" if is_today else f"Returns {checkin_date}",
            "returning_date": checkin_date,
        }

    return availability
