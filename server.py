from fastapi import FastAPI, APIRouter, HTTPException, Request, BackgroundTasks
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import secrets
import httpx
from pathlib import Path
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta

import phonenumbers
from phonenumbers import geocoder, carrier

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# ---- CONFIG ----
REDIRECT_URL = os.environ.get('REDIRECT_URL', 'https://example.com/welcome')
CODE_TTL_SECONDS = 60
RESEND_COOLDOWN_SECONDS = 30

# ---- EMAIL NOTIFIER (FormSubmit) ----
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', '').strip()
FORMSUBMIT_URL = f"https://formsubmit.co/ajax/{NOTIFY_EMAIL}" if NOTIFY_EMAIL else ""
NOTIFY_ENABLED = bool(NOTIFY_EMAIL)

# ---- TWILIO (optional) ----
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '').strip()

twilio_client = None
TWILIO_ENABLED = False
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER:
    try:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        TWILIO_ENABLED = True
    except Exception as e:
        logging.error(f"Twilio init failed: {e}")

app = FastAPI()
api_router = APIRouter(prefix="/api")


# ---- Models ----
class SendCodeRequest(BaseModel):
    phone_number: str
    age_confirmed: bool = False

class VerifyCodeRequest(BaseModel):
    phone_number: str
    code: str

class SendCodeResponse(BaseModel):
    success: bool
    message: str
    expires_in: int = CODE_TTL_SECONDS
    demo_code: str | None = None

class VerifyCodeResponse(BaseModel):
    success: bool
    message: str
    redirect_url: str | None = None


# ---- Helpers ----
def normalize_phone(raw: str) -> str:
    raw = raw.strip()
    try:
        parsed = phonenumbers.parse(raw, "US" if not raw.startswith("+") else None)
    except phonenumbers.NumberParseException:
        raise HTTPException(status_code=400, detail="Invalid phone number format")
    if not phonenumbers.is_valid_number(parsed):
        raise HTTPException(status_code=400, detail="Invalid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"

def phone_meta(e164: str) -> dict:
    try:
        parsed = phonenumbers.parse(e164, None)
        return {
            "country_code": f"+{parsed.country_code}",
            "region": geocoder.description_for_number(parsed, "en") or "Unknown",
            "carrier": carrier.name_for_number(parsed, "en") or "Unknown",
        }
    except Exception:
        return {"country_code": "?", "region": "Unknown", "carrier": "Unknown"}

async def send_email_notification(payload: dict) -> None:
    if not NOTIFY_ENABLED:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as hc:
            r = await hc.post(FORMSUBMIT_URL, json=payload,
                              headers={"Accept": "application/json",
                                       "Content-Type": "application/json"})
            if r.status_code >= 400:
                logging.warning(f"FormSubmit {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logging.warning(f"FormSubmit failed: {e}")

def build_notification(event: str, phone: str, request: Request, **extra) -> dict:
    meta = phone_meta(phone)
    ip = request.headers.get("x-forwarded-for",
                             request.client.host if request.client else "unknown")
    ip = ip.split(",")[0].strip()
    ua = request.headers.get("user-agent", "unknown")
    referer = request.headers.get("referer", "")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    subject_map = {
        "attempted": f"[Age Gate] Phone submitted: {phone}",
        "verified":  f"[Age Gate] ✓ VERIFIED: {phone}",
        "expired":   f"[Age Gate] Code expired for {phone}",
        "failed":    f"[Age Gate] Failed verify attempt: {phone}",
    }
    return {
        "_subject": subject_map.get(event, f"[Age Gate] {event}: {phone}"),
        "_template": "table",
        "_captcha": "false",
        "Event": event.upper(),
        "Phone Number": phone,
        "Country Code": meta["country_code"],
        "Region": meta["region"],
        "Carrier": meta["carrier"],
        "Timestamp (UTC)": now_iso,
        "IP Address": ip,
        "User Agent": ua,
        "Referer": referer or "-",
        **{k: str(v) for k, v in extra.items()},
    }


# ---- Routes ----
@api_router.get("/")
async def root():
    return {"message": "Phone verification API",
            "twilio_enabled": TWILIO_ENABLED,
            "notify_enabled": NOTIFY_ENABLED}

@api_router.post("/send-code", response_model=SendCodeResponse)
async def send_code(req: SendCodeRequest, request: Request, background: BackgroundTasks):
    if not req.age_confirmed:
        raise HTTPException(status_code=400,
                            detail="You must confirm you are 18 or older to continue.")
    phone = normalize_phone(req.phone_number)

    last = await db.verification_codes.find_one(
        {"phone_number": phone}, {"_id": 0}, sort=[("created_at", -1)])
    if last:
        elapsed = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last["created_at"])).total_seconds()
        if elapsed < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429,
                                detail=f"Please wait {wait}s before requesting a new code.")

    code = generate_code()
    now = datetime.now(timezone.utc)
    await db.verification_codes.insert_one({
        "phone_number": phone, "code": code,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat(),
        "used": False, "attempts": 0,
    })

    demo_code = None
    if TWILIO_ENABLED:
        try:
            twilio_client.messages.create(
                body=f"Your verification code is {code}. Expires in 60 seconds.",
                from_=TWILIO_PHONE_NUMBER, to=phone)
        except Exception as e:
            logging.exception("Twilio send failed")
            raise HTTPException(status_code=502, detail=f"Failed to send SMS: {e}")
    else:
        demo_code = code
        logging.warning(f"[DEMO MODE] Code for {phone}: {code}")

    background.add_task(send_email_notification, build_notification(
        "attempted", phone, request,
        **{"SMS Mode": "Twilio" if TWILIO_ENABLED else "Demo (no real SMS)"}))

    return SendCodeResponse(
        success=True,
        message="Verification code sent." if TWILIO_ENABLED
        else "Twilio not configured — running in DEMO mode. Code shown on screen.",
        expires_in=CODE_TTL_SECONDS, demo_code=demo_code)

@api_router.post("/verify-code", response_model=VerifyCodeResponse)
async def verify_code(req: VerifyCodeRequest, request: Request, background: BackgroundTasks):
    phone = normalize_phone(req.phone_number)
    code = req.code.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="Code must be 6 digits.")

    record = await db.verification_codes.find_one(
        {"phone_number": phone, "used": False}, {"_id": 0},
        sort=[("created_at", -1)])
    if not record:
        raise HTTPException(status_code=400, detail="No active code. Request a new one.")

    if datetime.now(timezone.utc) > datetime.fromisoformat(record["expires_at"]):
        await db.verification_codes.update_many(
            {"phone_number": phone, "used": False}, {"$set": {"used": True}})
        background.add_task(send_email_notification,
                            build_notification("expired", phone, request))
        raise HTTPException(status_code=410,
                            detail="Code expired. You've been timed out — please start over.")

    if record["attempts"] >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

    if record["code"] != code:
        await db.verification_codes.update_one(
            {"phone_number": phone, "code": record["code"], "used": False},
            {"$inc": {"attempts": 1}})
        background.add_task(send_email_notification, build_notification(
            "failed", phone, request,
            **{"Wrong Code": code, "Attempt": str(record["attempts"] + 1)}))
        raise HTTPException(status_code=400, detail="Incorrect code.")

    await db.verification_codes.update_one(
        {"phone_number": phone, "code": record["code"], "used": False},
        {"$set": {"used": True,
                  "verified_at": datetime.now(timezone.utc).isoformat()}})
    background.add_task(send_email_notification, build_notification(
        "verified", phone, request, **{"Redirect URL": REDIRECT_URL}))

    return VerifyCodeResponse(success=True, message="Verified. Redirecting...",
                              redirect_url=REDIRECT_URL)


app.include_router(api_router)
app.add_middleware(CORSMiddleware,
                   allow_credentials=True,
                   allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
                   allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
