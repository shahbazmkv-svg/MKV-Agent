import os
import re
import requests
import anthropic
from datetime import date, timedelta
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

MKV_FLEET_URL           = "https://www.mkvluxury.com/cars"

APPIC_URL               = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
APPIC_KEY               = os.environ["APPIC_API_KEY"]

GALLABOX_API_KEY        = os.environ["GALLABOX_API_KEY"]
GALLABOX_SECRET         = os.environ["GALLABOX_SECRET"]
GALLABOX_CHANNEL_GREEN  = os.environ["GALLABOX_CHANNEL_ID_Green"]
GALLABOX_CHANNEL_ORANGE = os.environ["GALLABOX_CHANNEL_ID_Orange"]

claude                  = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])

ADMIN_NUMBERS = {
    "+971562794545",
    "+971529409280",
}


# ─────────────────────────────────────────────
# SOURCE 1 — PRICING from mkvluxury.com
# ─────────────────────────────────────────────

def scrape_mkv_pricing():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MKV-Agent/1.0)"}
    try:
        resp = requests.get(MKV_FLEET_URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[scrape_mkv_pricing] ERROR: {e}")
        return []

    soup     = BeautifulSoup(resp.text, "html.parser")
    vehicles = []

    # Each vehicle card has an h5 name and h4 price block
    # Structure: h5 = name, h4 = "10999 AED7999 AED/ Day" (both prices in one tag)
    for h5 in soup.select("h5"):
        try:
            name = h5.get_text(strip=True)
            if not name:
                continue

            # Category is in the <p> before the h5
            cat_el = h5.find_previous("p")
            category = cat_el.get_text(strip=True) if cat_el else "Luxury"

            # Price is in the h4 after the h5
            h4 = h5.find_next("h4")
            if not h4:
                continue

            # h4 text looks like: "10999 AED7999 AED/ Day"
            price_text = h4.get_text(strip=True)
            nums = re.findall(r"\d+", price_text)
            if len(nums) < 2:
                continue

            orig = int(nums[0])
            disc = int(nums[1])

            if orig <= disc or disc == 0:
                continue

            disc_pct = round((orig - disc) / orig * 100)

            # Get the car slug from nearest parent <a href="/car/...">
            parent_a = h5.find_parent("a", href=re.compile(r"^/car/"))
            if not parent_a:
                # try finding the Reserve link nearby
                reserve = h5.find_next("a", href=re.compile(r"^/car/"))
                if not reserve:
                    continue
                slug = reserve["href"].replace("/car/", "").strip("/")
            else:
                slug = parent_a["href"].replace("/car/", "").strip("/")

            vehicles.append({
                "name":             name,
                "category":         category,
                "original_price":   orig,
                "discounted_price": disc,
                "discount_pct":     disc_pct,
                "slug":             slug,
                "url":              f"https://www.mkvluxury.com/car/{slug}",
                "km_included":      200,
            })

        except Exception:
            continue

    print(f"[scrape_mkv_pricing] {len(vehicles)} vehicles parsed successfully")
    return vehicles


# ─────────────────────────────────────────────
# SOURCE 2 — AVAILABILITY from Appic Fleet API
# ─────────────────────────────────────────────

def fetch_appic_data(direction, start, end):
    try:
        resp = requests.post(
            APPIC_URL,
            data={
                "key":       APPIC_KEY,
                "startDate": start,
                "endDate":   end,
                "direction": direction,
            },
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[fetch_appic_data] direction={direction} count={result.get('count', '?')}")

        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            if result.get("issuccess") is False:
                print(f"[fetch_appic_data] API error: {result.get('message')}")
                return []
            for key in ("data", "records", "vehicles", "results"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return []
    except Exception as e:
        print(f"[fetch_appic_data] ERROR direction={direction}: {e}")
        return []


def get_availability_from_appic():
    today     = date.today().strftime("%Y-%m-%d")
    next_week = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")

    checked_out = fetch_appic_data("Out", today, next_week)
    checked_in  = fetch_appic_data("In",  today, next_week)

    availability = {}

    # Mark checked-out vehicles as unavailable — Active contracts only
    for record in checked_out:
        name     = record.get("vehicleName", "").strip().lower()
        contract = record.get("contractID", "")
        if not name or "Draft" in contract:
            continue
        ret_date = record.get("endDate", next_week)
        availability[name] = {
            "available":      False,
            "status":         "Checked out",
            "returning_date": ret_date,
        }

    # Mark returning vehicles — Active contracts only
    for record in checked_in:
        name     = record.get("vehicleName", "").strip().lower()
        contract = record.get("contractID", "")
        if not name or "Draft" in contract:
            continue
        checkin_date = record.get("endDate", today)
        is_today     = checkin_date == today
        availability[name] = {
            "available":      is_today,
            "status":         "Returning today" if is_today else f"Returns {checkin_date}",
            "returning_date": checkin_date,
        }

    print(f"[get_availability_from_appic] {len(availability)} vehicles tracked")
    return availability


# ─────────────────────────────────────────────
# MERGE — combine both sources
# ─────────────────────────────────────────────

def merge_fleet(mkv_vehicles, appic_avail):
    skip_words = {"the", "and", "for", "2024", "2025", "2026"}
    merged     = []

    for v in mkv_vehicles:
        name_upper = v["name"].upper().strip()
        mkv_words  = set(
            w for w in name_upper.split()
            if len(w) > 3 and w.lower() not in skip_words
        )
        avail_data = None

        for appic_name, a in appic_avail.items():
            appic_words = set(
                w for w in appic_name.upper().split()
                if len(w) > 3 and w.lower() not in skip_words
            )
            overlap = mkv_words & appic_words
            if len(overlap) >= 2 or (len(overlap) == 1 and len(mkv_words) <= 2):
                avail_data = a
                print(f"[merge] MATCHED '{v['name']}' -> '{appic_name}' via {overlap}")
                break

        if avail_data:
            v["available"]      = avail_data["available"]
            v["status"]         = avail_data["status"]
            v["returning_date"] = avail_data["returning_date"]
        else:
            v["available"]      = True
            v["status"]         = "Available"
            v["returning_date"] = None

        merged.append(v)

    available_count = sum(1 for v in merged if v["available"])
    print(f"[merge_fleet] {available_count}/{len(merged)} vehicles available")
    return merged


# ─────────────────────────────────────────────
# AI PROMPTS — customer vs admin
# ─────────────────────────────────────────────

CUSTOMER_SYSTEM = """
You are the MKV Luxury WhatsApp concierge in Dubai.
Help customers find and book the perfect luxury or supercar.

Rules:
- Always show the discounted AED price only (never the original)
- Mention discount % as a selling point
- Suggest maximum 3 vehicles per reply — best match first
- Only suggest vehicles that are currently available
- If a requested car is unavailable, mention when it returns and suggest an alternative
- Keep reply under 130 words — warm, aspirational, confident
- Always end with: "Reserve now at mkvluxury.com or reply to book"
- Mention zero deposit and door-to-door delivery when relevant
- 200 km included per day on all vehicles
"""

ADMIN_SYSTEM = """
You are MKV Luxury's internal fleet intelligence assistant.
Provide complete operational data for staff queries.

For each vehicle include:
- Full name and category
- Original price vs discounted price (AED) + discount %
- Availability status and returning date
- Revenue opportunity (discounted price x available units if known)
- Flag any vehicle checked out with no return date

Format as a clean operational report. Be precise and data-driven.
Pricing source: mkvluxury.com (live)
Availability source: Appic Fleet API (live)
"""


def build_customer_prompt(msg, fleet):
    available   = [v for v in fleet if v["available"]]
    coming_soon = [v for v in fleet if not v["available"] and v.get("returning_date")]

    avail_lines = "\n".join([
        f"- {v['name']} ({v['category']}): AED {v['discounted_price']:,}/day "
        f"[{v['discount_pct']}% OFF] — {v['url']}"
        for v in available
    ]) or "No vehicles currently available"

    soon_lines = "\n".join([
        f"- {v['name']}: returns {v['returning_date']}"
        for v in coming_soon[:5]
    ]) or "None"

    return f"""MKV Luxury — live fleet status today:

AVAILABLE NOW:
{avail_lines}

RETURNING SOON:
{soon_lines}

Customer WhatsApp message: "{msg}"

Reply as MKV's luxury concierge."""


def build_admin_prompt(msg, fleet):
    lines = "\n".join([
        f"- {v['name']} | {v['category']} | "
        f"Original: AED {v['original_price']:,} -> Discounted: AED {v['discounted_price']:,} "
        f"({v['discount_pct']}% OFF) | "
        f"Status: {v['status']} | "
        f"Returning: {v['returning_date'] or 'N/A'} | "
        f"Link: {v['url']}"
        for v in fleet
    ])

    available_count = sum(1 for v in fleet if v["available"])
    total           = len(fleet)

    return f"""MKV Fleet Snapshot — {date.today()}
Pricing: mkvluxury.com (live scrape)
Availability: Appic Fleet API (live)
Summary: {available_count} available / {total} total vehicles

{lines}

Staff query: "{msg}"
"""


# ─────────────────────────────────────────────
# AI CALL
# ─────────────────────────────────────────────

def get_ai_response(system, prompt):
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        print(f"[get_ai_response] ERROR: {e}")
        return "Sorry, I'm having a technical issue. Please call us at +971 58 665 6085."


# ─────────────────────────────────────────────
# GALLABOX SENDER
# ─────────────────────────────────────────────

def send_whatsapp(to, body, channel_id):
    try:
        resp = requests.post(
            "https://server.gallabox.com/devapi/messages/whatsapp",
            headers={
                "apiKey":       GALLABOX_API_KEY,
                "apiSecret":    GALLABOX_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "channelId":   channel_id,
                "channelType": "whatsapp",
                "recipient":   {"phone": to},
                "whatsapp":    {"type": "text", "text": {"body": body}}
            },
            timeout=8
        )
        print(f"[send_whatsapp] to={to} channel={channel_id} status={resp.status_code}")
    except Exception as e:
        print(f"[send_whatsapp] ERROR: {e}")


# ─────────────────────────────────────────────
# WEBHOOK — main entry point
# ─────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        print(f"[webhook] payload: {data}")

        phone = (
            data.get("contacts", [{}])[0].get("phone")
            or data.get("from")
            or ""
        )

        msg = (
            data.get("messages", [{}])[0].get("text", {}).get("body")
            or data.get("text", {}).get("body")
            or data.get("message", "")
            or ""
        ).strip()

        if not phone or not msg:
            return jsonify({"status": "ignored", "reason": "no phone or message"}), 200

        incoming_channel = (
            data.get("channel", {}).get("id")
            or data.get("channelId")
            or GALLABOX_CHANNEL_GREEN
        )

        print(f"[webhook] from={phone} channel={incoming_channel} msg={msg}")

        mkv_vehicles = scrape_mkv_pricing()
        appic_avail  = get_availability_from_appic()
        fleet        = merge_fleet(mkv_vehicles, appic_avail)

        if phone in ADMIN_NUMBERS:
            prompt = build_admin_prompt(msg, fleet)
            reply  = get_ai_response(ADMIN_SYSTEM, prompt)
        else:
            prompt = build_customer_prompt(msg, fleet)
            reply  = get_ai_response(CUSTOMER_SYSTEM, prompt)

        send_whatsapp(phone, reply, incoming_channel)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"[webhook] UNHANDLED ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "MKV AI Agent is running",
        "date":    str(date.today()),
        "version": "2.0"
    }), 200


# ─────────────────────────────────────────────
# LOCAL TEST
# ─────────────────────────────────────────────

def run_test():
    print("\n=== TEST 1: MKV Pricing ===")
    vehicles = scrape_mkv_pricing()
    for v in vehicles[:5]:
        print(f"  {v['name']} | AED {v['discounted_price']:,}/day | {v['discount_pct']}% OFF")

    print("\n=== TEST 2: Appic Checkout ===")
    today     = date.today().strftime("%Y-%m-%d")
    next_week = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    out_data  = fetch_appic_data("Out", today, next_week)
    print(f"  Checked out records: {len(out_data)}")
    if out_data:
        print(f"  Sample: {out_data[0]}")

    print("\n=== TEST 3: Appic Check-in ===")
    in_data = fetch_appic_data("In", today, next_week)
    print(f"  Check-in records: {len(in_data)}")
    if in_data:
        print(f"  Sample: {in_data[0]}")

    print("\n=== TEST 4: Merged Fleet ===")
    avail  = get_availability_from_appic()
    fleet  = merge_fleet(vehicles, avail)
    avail_count = sum(1 for v in fleet if v["available"])
    print(f"  {avail_count}/{len(fleet)} available")
    for v in fleet:
        print(f"  {v['name']} — {v['status']} | AED {v['discounted_price']:,}/day")

    print("\n=== ALL TESTS COMPLETE ===\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        run_test()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
