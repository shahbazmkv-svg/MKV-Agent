import os
import re
import time
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
MKV_CAR_BASE            = "https://www.mkvluxury.com/car"

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

PAYMENT_KEYWORDS = [
    "proceed", "ready to pay", "how to pay", "payment",
    "pay now", "send qr", "10%", "yes proceed", "confirm payment",
    "i want to pay", "make payment", "pay deposit",
]

BOOKING_KEYWORDS = [
    "book", "reserve", "confirm", "i want to book", "book this",
    "book on", "reserve for", "i'll take", "i want the", "take this",
    "proceed with", "go ahead", "yes book", "book it",
]

DETAIL_KEYWORDS = [
    "extra km", "add-on", "addon", "baby seat", "protect",
    "weekly", "monthly", "per week", "per month", "how much",
    "price", "cost", "deposit",
]

QR_CODE_URL = "https://raw.githubusercontent.com/shahbazmkv-svg/MKV-Agent/main/Nomod_QR_code.jpeg"

# Brand-only words — must NOT match alone, need model word overlap
BRAND_ONLY_WORDS = {
    "FERRARI", "MERCEDES", "PORSCHE", "AUDI", "BMW", "LAMBORGHINI",
    "BENTLEY", "ROLLS", "ROYCE", "RANGE", "ROVER", "LAND", "FORD",
    "TOYOTA", "NISSAN", "CHEVROLET", "CADILLAC", "LEXUS", "JEEP",
    "MCLAREN", "LOTUS", "MORGAN", "ASTON", "MARTIN", "BENZ", "AMG",
}

# ─────────────────────────────────────────────
# CACHE — avoids re-fetching MKV on every message
# ─────────────────────────────────────────────

_cache = {
    "vehicles":   [],
    "fetched_at": 0,
}
CACHE_TTL = 600  # 10 minutes


def get_cached_vehicles():
    now = time.time()
    if now - _cache["fetched_at"] > CACHE_TTL or not _cache["vehicles"]:
        vehicles = scrape_mkv_pricing()
        _cache["vehicles"]   = vehicles
        _cache["fetched_at"] = now
        print(f"[cache] refreshed — {len(vehicles)} vehicles")
    else:
        print(f"[cache] using cached — {len(_cache['vehicles'])} vehicles")
    return _cache["vehicles"]


# ─────────────────────────────────────────────
# SOURCE 1A — LIST PAGE pricing
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
    seen     = set()

    for card in soup.select("a[href^='/car/']"):
        try:
            slug = card["href"].replace("/car/", "").strip("/")
            if not slug or slug in seen:
                continue

            name_el = card.select_one("h5")
            if not name_el:
                continue

            name = name_el.get_text(strip=True)
            if not name or len(name) < 3:
                continue

            cat_el   = card.select_one("p")
            category = cat_el.get_text(strip=True) if cat_el else "Luxury"

            # Prices strictly inside this card
            h4s = card.select("h4")
            if len(h4s) < 2:
                continue

            def parse_aed(el):
                return int(re.sub(r"[^\d]", "", el.get_text()))

            orig = parse_aed(h4s[0])
            disc = parse_aed(h4s[1])

            if orig <= 0 or disc <= 0 or orig <= disc:
                continue

            disc_pct = round((orig - disc) / orig * 100)

            seen.add(slug)
            vehicles.append({
                "name":             name,
                "category":         category,
                "original_price":   orig,
                "discounted_price": disc,
                "discount_pct":     disc_pct,
                "slug":             slug,
                "url":              f"{MKV_CAR_BASE}/{slug}",
                "km_included":      200,
                "weekly_price":     None,
                "monthly_price":    None,
                "extra_km_rate":    None,
                "security_deposit": None,
                "zero_deposit_fee": None,
                "baby_seat_price":  None,
                "total_protect":    None,
                "rim_tyre":         None,
                "baby_seat":        None,
            })
        except Exception:
            continue

    print(f"[scrape_mkv_pricing] {len(vehicles)} vehicles found")
    return vehicles


# ─────────────────────────────────────────────
# SOURCE 1B — DETAIL PAGE per vehicle
# ─────────────────────────────────────────────

def scrape_vehicle_detail(slug):
    url     = f"{MKV_CAR_BASE}/{slug}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MKV-Agent/1.0)"}
    detail  = {}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        m = re.search(r"([\d,]+)\s*AED\s*/\s*week", text, re.I)
        if m:
            detail["weekly_price"] = int(m.group(1).replace(",", ""))

        m = re.search(r"([\d,]+)\s*AED\s*/\s*month", text, re.I)
        if m:
            detail["monthly_price"] = int(m.group(1).replace(",", ""))

        m = re.search(r"(\d+)\s*AED\s*/\s*[Kk][Mm]", text)
        if m:
            detail["extra_km_rate"] = int(m.group(1))

        m = re.search(r"[Dd]eposit[\s\S]{0,30}?AED\s*([\d,]+)", text)
        if m:
            detail["security_deposit"] = int(m.group(1).replace(",", ""))

        m = re.search(r"[Zz]ero\s*[Dd]eposit[\s\S]{0,50}?AED\s*(\d+)\s*/\s*day", text)
        if m:
            detail["zero_deposit_fee"] = int(m.group(1))
        else:
            m = re.search(r"[Dd]eposit.free[\s\S]{0,30}?AED\s*(\d+)", text)
            if m:
                detail["zero_deposit_fee"] = int(m.group(1))

        m = re.search(r"[Bb]aby\s*[Ss]eat[\s\S]{0,30}?AED\s*(\d+)", text)
        if m:
            detail["baby_seat_price"] = int(m.group(1))

        detail["total_protect"] = bool(re.search(r"Total Protect", text, re.I))
        detail["rim_tyre"]      = bool(re.search(r"Rim.{0,5}Tyre", text, re.I))
        detail["baby_seat"]     = bool(re.search(r"Baby Seat", text, re.I))

        m = re.search(r"(\d{3,4})\s*KM\s*/\s*(day|week)", text, re.I)
        if m:
            detail["km_included"] = int(m.group(1))

    except Exception as e:
        print(f"[scrape_vehicle_detail] ERROR {slug}: {e}")

    return detail


def enrich_fleet_with_details(vehicles):
    for v in vehicles[:20]:
        detail = scrape_vehicle_detail(v["slug"])
        for key, val in detail.items():
            if val is not None:
                v[key] = val
    print(f"[enrich_fleet] detail pages fetched")
    return vehicles


# ─────────────────────────────────────────────
# SOURCE 2 — AVAILABILITY from Appic
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
# MERGE — strict model-word matching only
# ─────────────────────────────────────────────

def merge_fleet(mkv_vehicles, appic_avail):
    skip_words = {"the", "and", "for", "2024", "2025", "2026", "new"}
    merged     = []

    for v in mkv_vehicles:
        name_upper = v["name"].upper().strip()
        mkv_words  = set(
            w for w in name_upper.split()
            if len(w) > 2 and w.lower() not in skip_words
        )
        # Model-specific words only — strip brand names completely
        mkv_model  = mkv_words - BRAND_ONLY_WORDS

        avail_data = None
        best_score = 0
        best_appic = ""

        for appic_name, a in appic_avail.items():
            appic_upper = appic_name.upper()
            appic_words = set(
                w for w in appic_upper.split()
                if len(w) > 2 and w.lower() not in skip_words
            )
            appic_model = appic_words - BRAND_ONLY_WORDS

            # STRICT: brand words alone never trigger a match
            # Only model-specific words count
            model_overlap = mkv_model & appic_model
            if not model_overlap:
                continue

            score = len(model_overlap)
            if score > best_score:
                best_score = score
                avail_data = a
                best_appic = appic_name

        if avail_data and best_score >= 1:
            v["available"]      = avail_data["available"]
            v["status"]         = avail_data["status"]
            v["returning_date"] = avail_data["returning_date"]
            print(f"[merge] MATCHED '{v['name']}' -> '{best_appic}'")
        else:
            v["available"]      = True
            v["status"]         = "Available"
            v["returning_date"] = None

        merged.append(v)

    available_count = sum(1 for v in merged if v["available"])
    print(f"[merge_fleet] {available_count}/{len(merged)} vehicles available")
    return merged


# ─────────────────────────────────────────────
# FORMAT vehicle for prompt
# ─────────────────────────────────────────────

def format_vehicle_pricing(v, show_details=False):
    lines = []
    lines.append(f"Name: {v['name']}")
    lines.append(f"Category: {v['category']}")
    lines.append(f"Daily price: AED {v['discounted_price']:,}/day [{v['discount_pct']}% OFF]")
    lines.append(f"Status: {v['status']}")
    if v.get("returning_date"):
        lines.append(f"Returning: {v['returning_date']}")
    lines.append(f"Included KM: {v.get('km_included', 200)} km/day")

    if v.get("zero_deposit_fee"):
        lines.append(f"Zero deposit option: AED {v['zero_deposit_fee']}/day")
    if v.get("security_deposit"):
        lines.append(f"Security deposit: AED {v['security_deposit']:,} (refundable within 21 days)")

    if show_details:
        if v.get("extra_km_rate"):
            lines.append(f"Extra KM rate: AED {v['extra_km_rate']}/km")
        else:
            lines.append("Extra KM rate: AED 40/km")
        if v.get("weekly_price"):
            lines.append(f"Weekly price: AED {v['weekly_price']:,}/week")
        if v.get("monthly_price"):
            lines.append(f"Monthly price: AED {v['monthly_price']:,}/month")
        addons = []
        if v.get("total_protect"):
            addons.append("Total Protection (paid add-on)")
        if v.get("rim_tyre"):
            addons.append("Rim+Tyre+Windscreen Protection (paid add-on)")
        if v.get("baby_seat"):
            price = v.get("baby_seat_price")
            if price:
                addons.append(f"Baby Seat — AED {price}/day")
            else:
                addons.append("Baby Seat (paid add-on — team will confirm rate)")
        if addons:
            lines.append(f"Paid add-ons: {', '.join(addons)}")

    lines.append("VAT: 5% applied at checkout")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# AI PROMPTS
# ─────────────────────────────────────────────

CUSTOMER_SYSTEM = """
You are Kathy, MKV Luxury's WhatsApp concierge in Dubai.
You help customers find and book luxury and supercars.
Personality: warm, professional, aspirational — like a 5-star hotel concierge.

GREETING RULE:
When customer sends a greeting ("Hi", "Hello", "Hey", "Good morning", "Salam" etc),
respond with EXACTLY this message word for word:

"Greetings from MKV Luxury! 🚗✨ We're excited to assist you and ensure a smooth, unforgettable luxury car rental experience.

I'm Kathy, your personal concierge. Whether you're looking for a Ferrari, Lamborghini, Rolls Royce, or something special — we deliver straight to your door with zero deposit and 200 km daily included.

What type of vehicle interests you today, or would you like to see our top picks?

Reserve now at mkvluxury.com or reply to book 🚗"

Do NOT list prices or specific vehicles in the greeting.

PRICING RULE:
When sharing vehicle pricing include:
- Daily price (discounted AED only, never original)
- Discount % as a selling point
- Included KM per day
- Deposit options: zero deposit fee per day OR refundable security deposit within 21 days
- 5% VAT applied at checkout
- Basic insurance included

Do NOT mention extra KM rate or add-ons unless customer specifically asks.
Only share weekly/monthly price if customer specifically asks for it.
Never mention original price.
NEVER include vehicle URLs or links in replies — no exceptions.

Example format:
"McLaren Artura Spider 2025 — AED 999/day [23% OFF]
200 km/day included
Zero deposit: AED 200/day OR Security deposit: AED 5,000 (refundable in 21 days)
Basic insurance included | + 5% VAT at checkout"

BABY SEAT RULE:
Baby seat is a PAID add-on — never free or complimentary.
When customer asks about baby seat:
- Share exact price per day if available from vehicle data
- If no price available say: "Baby seat available as a paid add-on — our team will confirm the rate"
- NEVER say baby seat is free or complimentary

BUDGET FILTER RULE:
When customer asks for cars within a budget (e.g. "under AED 1000", "budget 500 a day"):
- List ALL available vehicles within that budget from the fleet data
- Do not limit to 3 — show all matching options
- Sort cheapest first

CAR CHOICE CONFIRMATION RULE:
When customer confirms a vehicle choice ("I want the Ferrari", "Book the Urus",
"I'll take the McLaren", "that one", "yes", "perfect", "looks good") respond with
EXACTLY this, replacing [CAR NAME] with the chosen vehicle:

"Excellent choice! 🌟 The [CAR NAME] is a fantastic pick.

Could you please let us know the date you'd like to book the car and the number of days? This will help us check availability and confirm your reservation right away."

Do NOT ask for documents yet at this stage.

OFFER/FLYER RULE:
When customer shares a promotional image, flyer, or offer screenshot respond with:

"Thank you for sharing that! 🚗 Great to see you're interested in this offer.

Could you please let us know:
Your preferred booking date?
Number of days you'd like to rent?

Once confirmed, I'll check availability and connect you with our reservations team right away!"

BOOKING TRIGGER RULE:
When customer provides dates and number of days AND confirms they want to proceed —
respond with EXACTLY this, no variations, do NOT suggest other vehicles:

"Perfect! 🚗 The process is simple!

To secure your booking reservation, kindly provide the following:

1️⃣ Driver's license (front & back)
2️⃣ Passport + stamp page OR Emirates ID (front & back)
3️⃣ Home country address
4️⃣ Local UAE address
5️⃣ Email ID
6️⃣ WhatsApp / alternative number

💳 A minimum 10% advance payment is required to confirm your booking. The remaining balance is settled upon delivery. Extensions must be paid on the same day — no credit extensions.

Reply with your details and we'll get everything arranged! 🌟"

DOCUMENT SUBMISSION RULE:
When customer shares ANY of the following after a booking request:
- Email address (contains @ symbol)
- Location or address (Dubai, UK, Abu Dhabi, any city or country)
- ID, passport, or license details
- Sends an image or photo
- Says done, sent, here, check, what next, ok

Respond with EXACTLY this — do NOT restart, do NOT suggest vehicles, do NOT ask questions:

"Thank you! 🌟 We have received your details.

A reservation specialist will connect with you shortly to process your 10% advance payment via Nomod and arrange delivery of your vehicle.

We look forward to making your luxury experience unforgettable! 🚗✨"

PAYMENT METHODS RULE:
When customer asks about payment methods, how to pay, or payment options respond with:

"We accept the following payment methods 💳

✅ Credit / Debit Card
✅ Cash (AED)
✅ Bank Transfer
✅ Crypto (USDT / Bitcoin)
✅ Nomod QR (10% advance to confirm booking)

Our reservation specialist will guide you through the payment process once your booking is confirmed. 🚗"

PAYMENT TRIGGER RULE:
When customer says proceed, ready to pay, how to pay, payment, pay now,
10%, yes proceed, confirm payment, send payment link respond with EXACTLY this:

"Thank you for confirming! 💳

Please scan the QR code to process your 10% advance payment via Nomod.

Once payment is completed, kindly share the payment confirmation screenshot and our reservation specialist will arrange your vehicle delivery immediately.

MKV Car Rental LLC"

AVAILABILITY CHECK RULE:
When customer asks about a specific vehicle for specific dates:
- If vehicle is in AVAILABLE NOW list confirm available and share pricing
- If NOT AVAILABLE say when it returns and suggest top 2 alternatives with pricing
- Never confirm a vehicle that shows as Checked out

RENTAL DURATION RULE:
MKV Luxury bills in full 24-hour periods. Rules:
- Minimum rental is 1 day (24 hours)
- Any time beyond a full 24-hour period counts as a new full day
- Examples:
  * Pickup 11 AM, return 11 AM next day = 1 day ✅
  * Pickup 11 AM, return 11 PM same day = 1 day (under 24 hrs = 1 day minimum)
  * Pickup 11 AM Day 1, return 11 PM Day 2 = 2 days (36 hrs = beyond 24 hrs = 2 days)
  * Pickup 10 AM, return 6 PM next day = 2 days (32 hrs = beyond 24 hrs = 2 days)
- When customer provides pickup and return times, always calculate the correct number of billing days
- Inform customer clearly: "Our rentals are billed in full 24-hour periods. Your rental from [time] to [time] will be billed as [X] days."
- Never quote a partial day rate

GENERAL RULES:
- Always show discounted AED daily price only (never original price)
- Mention discount % as a selling point
- For general queries suggest maximum 3 vehicles — best match first
- For budget queries show ALL vehicles within budget
- Only suggest available vehicles
- Basic insurance included with every rental
- Full insurance and Total Protection is a PAID add-on — NEVER say it is free
- Baby seat is a PAID add-on — NEVER say it is free or complimentary
- Do NOT mention extra KM charges or add-ons unless customer asks
- NEVER include vehicle URLs or links in replies
- Keep replies under 160 words unless it is a booking or document or payment message
- Always end general replies with: Reserve now at mkvluxury.com or reply to book 🚗
- Use 1-2 emojis per message — keep it premium not casual
- NEVER restart the greeting mid-conversation
- NEVER offer new vehicle choices after customer has started booking process
- NEVER repeat the welcome message if conversation is already ongoing
"""

ADMIN_SYSTEM = """
You are MKV Luxury's internal fleet intelligence assistant.
Provide complete operational data for staff queries.

For each vehicle include:
- Full name, category, URL
- Original price vs discounted price (AED) and discount %
- Weekly and monthly rates if available
- Availability status and returning date
- Security deposit and zero deposit fee
- Extra KM rate
- Baby seat price if available
- Revenue opportunity: daily price x available units
- Flag any vehicle checked out with no return date

Format as a clean operational report. Be precise and data-driven.
Pricing source: mkvluxury.com (live)
Availability source: Appic Fleet API (live)
"""


def build_customer_prompt(msg, fleet, show_details=False):
    available   = [v for v in fleet if v["available"]]
    coming_soon = [v for v in fleet if not v["available"] and v.get("returning_date")]

    avail_lines = "\n\n".join([
        format_vehicle_pricing(v, show_details=show_details) for v in available
    ]) or "No vehicles currently available"

    soon_lines = "\n".join([
        f"- {v['name']}: returns {v['returning_date']}"
        for v in coming_soon[:5]
    ]) or "None"

    return (
        f"MKV Luxury live fleet status today ({date.today()}):\n\n"
        f"AVAILABLE NOW:\n{avail_lines}\n\n"
        f"RETURNING SOON:\n{soon_lines}\n\n"
        f"Customer WhatsApp message: \"{msg}\"\n\n"
        f"Reply as Kathy, MKV's luxury concierge. Follow all rules strictly."
    )


def build_admin_prompt(msg, fleet):
    lines = "\n".join([
        f"- {v['name']} | {v['category']} | "
        f"AED {v['original_price']:,} -> AED {v['discounted_price']:,} ({v['discount_pct']}% OFF) | "
        f"Weekly: AED {v.get('weekly_price', 'N/A')} | "
        f"Monthly: AED {v.get('monthly_price', 'N/A')} | "
        f"Extra KM: AED {v.get('extra_km_rate', '40')}/km | "
        f"Zero deposit: AED {v.get('zero_deposit_fee', 'N/A')}/day | "
        f"Security deposit: AED {v.get('security_deposit', 'N/A')} | "
        f"Baby seat: AED {v.get('baby_seat_price', 'N/A')}/day | "
        f"Status: {v['status']} | "
        f"Returning: {v['returning_date'] or 'N/A'} | "
        f"Link: {v['url']}"
        for v in fleet
    ])

    available_count = sum(1 for v in fleet if v["available"])
    total           = len(fleet)

    return (
        f"MKV Fleet Snapshot — {date.today()}\n"
        f"Pricing: mkvluxury.com (live)\n"
        f"Availability: Appic Fleet API (live)\n"
        f"Summary: {available_count} available / {total} total\n\n"
        f"{lines}\n\n"
        f"Staff query: \"{msg}\""
    )


# ─────────────────────────────────────────────
# AI CALL
# ─────────────────────────────────────────────

def get_ai_response(system, prompt):
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        print(f"[get_ai_response] ERROR: {e}")
        return "Sorry, I'm having a technical issue. Please call us at +971 56 279 4545."


# ─────────────────────────────────────────────
# GALLABOX SENDERS
# ─────────────────────────────────────────────

def send_whatsapp(to, body, channel_id):
    try:
        clean_phone = to.replace("+", "").replace(" ", "").strip()
        resp = requests.post(
            "https://server.gallabox.com/devapi/messages/whatsapp",
            headers={
                "apiKey":       GALLABOX_API_KEY,
                "apiSecret":    GALLABOX_SECRET,
                "Content-Type": "application/json",
            },
            json={
                "channelId":   channel_id,
                "channelType": "whatsapp",
                "recipient":   {"phone": clean_phone, "name": "Customer"},
                "whatsapp":    {"type": "text", "text": {"body": body}},
            },
            timeout=8,
        )
        print(f"[send_whatsapp] to={clean_phone} status={resp.status_code}")
        print(f"[send_whatsapp] response={resp.text}")
    except Exception as e:
        print(f"[send_whatsapp] ERROR: {e}")


def send_whatsapp_image(to, caption, image_url, channel_id):
    try:
        clean_phone = to.replace("+", "").replace(" ", "").strip()
        resp = requests.post(
            "https://server.gallabox.com/devapi/messages/whatsapp",
            headers={
                "apiKey":       GALLABOX_API_KEY,
                "apiSecret":    GALLABOX_SECRET,
                "Content-Type": "application/json",
            },
            json={
                "channelId":   channel_id,
                "channelType": "whatsapp",
                "recipient":   {"phone": clean_phone, "name": "Customer"},
                "whatsapp": {
                    "type":  "image",
                    "image": {"url": image_url, "caption": caption},
                },
            },
            timeout=8,
        )
        print(f"[send_image] to={clean_phone} status={resp.status_code}")
        print(f"[send_image] response={resp.text}")
    except Exception as e:
        print(f"[send_image] ERROR: {e}")


# ─────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        print(f"[webhook] payload: {data}")

        phone = (
            data.get("whatsapp", {}).get("from")
            or data.get("contact", {}).get("phone")
            or data.get("contacts", [{}])[0].get("phone")
            or data.get("from")
            or ""
        )
        if phone and not phone.startswith("+"):
            phone = "+" + phone

        msg = (
            data.get("whatsapp", {}).get("text", {}).get("body")
            or data.get("messages", [{}])[0].get("text", {}).get("body")
            or data.get("text", {}).get("body")
            or data.get("message", "")
            or ""
        ).strip()

        incoming_channel = (
            data.get("channelId")
            or data.get("channel", {}).get("id")
            or GALLABOX_CHANNEL_GREEN
        )

        print(f"[webhook] from={phone} channel={incoming_channel} msg={msg}")

        if not phone or not msg:
            return jsonify({"status": "ignored", "reason": "no phone or message"}), 200

        msg_lower  = msg.lower()
        is_booking = any(kw in msg_lower for kw in BOOKING_KEYWORDS)
        is_payment = any(kw in msg_lower for kw in PAYMENT_KEYWORDS)
        is_detail  = any(kw in msg_lower for kw in DETAIL_KEYWORDS)

        # Use cached vehicles for speed — only fetch every 10 mins
        import copy
        mkv_vehicles = copy.deepcopy(get_cached_vehicles())
        appic_avail  = get_availability_from_appic()
        fleet        = merge_fleet(mkv_vehicles, appic_avail)

        # Enrich with detail pages only if needed
        if is_detail:
            fleet = enrich_fleet_with_details(fleet)

        if phone in ADMIN_NUMBERS:
            prompt = build_admin_prompt(msg, fleet)
            reply  = get_ai_response(ADMIN_SYSTEM, prompt)
            send_whatsapp(phone, reply, incoming_channel)
        else:
            prompt = build_customer_prompt(msg, fleet, show_details=is_detail)
            reply  = get_ai_response(CUSTOMER_SYSTEM, prompt)
            send_whatsapp(phone, reply, incoming_channel)

            if is_booking or is_payment:
                send_whatsapp_image(
                    phone,
                    "Scan to pay your 10% advance — MKV Car Rental LLC 🚗",
                    QR_CODE_URL,
                    incoming_channel,
                )

        return jsonify({"status": "ok", "message": reply}), 200

    except Exception as e:
        print(f"[webhook] UNHANDLED ERROR: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":  "MKV AI Agent — Kathy is running",
        "date":    str(date.today()),
        "version": "5.5",
    }), 200


# ─────────────────────────────────────────────
# LOCAL TEST
# ─────────────────────────────────────────────

def run_test():
    print("\n=== TEST 1: MKV Pricing ===")
    vehicles = scrape_mkv_pricing()
    print(f"  {len(vehicles)} vehicles found")
    for v in vehicles[:5]:
        print(f"  {v['name']} | AED {v['discounted_price']:,}/day | {v['discount_pct']}% OFF")

    print("\n=== TEST 2: Detail Page ===")
    if vehicles:
        detail = scrape_vehicle_detail(vehicles[0]["slug"])
        print(f"  {vehicles[0]['slug']}: {detail}")

    print("\n=== TEST 3: Appic Checkout ===")
    today     = date.today().strftime("%Y-%m-%d")
    next_week = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    out_data  = fetch_appic_data("Out", today, next_week)
    print(f"  Checked out: {len(out_data)} records")

    print("\n=== TEST 4: Appic Check-in ===")
    in_data = fetch_appic_data("In", today, next_week)
    print(f"  Check-in: {len(in_data)} records")

    print("\n=== TEST 5: Merged Fleet ===")
    avail       = get_availability_from_appic()
    fleet       = merge_fleet(vehicles, avail)
    avail_count = sum(1 for v in fleet if v["available"])
    print(f"  {avail_count}/{len(fleet)} available")
    for v in fleet:
        if v["status"] != "Available":
            print(f"  {v['name']} — {v['status']} | AED {v['discounted_price']:,}/day")

    print("\n=== ALL TESTS COMPLETE ===\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        run_test()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
