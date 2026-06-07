from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from phonenumbers import PhoneNumberType, number_type
from fastapi import FastAPI, APIRouter, HTTPException, Request, BackgroundTasks
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import json
import logging
import secrets
import httpx
from pathlib import Path
from pydantic import BaseModel, EmailStr
from datetime import datetime, timezone, timedelta

import phonenumbers
from phonenumbers import geocoder, carrier

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# ---- MongoDB ----
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# ---- CONFIG ----
REDIRECT_URL = os.environ.get('REDIRECT_URL', 'https://example.com/welcome')
CODE_TTL_SECONDS = 300         # 5 minutes — email is slower than SMS
RESEND_COOLDOWN_SECONDS = 30
SITE_NAME = os.environ.get('SITE_NAME', 'Age Verification')

# ---- Disposable email blocker ----
DISPOSABLE_EMAIL_DOMAINS: set[str] = set()

def assert_email_not_disposable(email: str) -> None:
    domain = email.split("@", 1)[-1].lower().strip()
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail="Disposable / temporary email addresses are not allowed. "
                   "Please use your real email.",
        )

# ---- ADMIN EMAIL NOTIFIER (FormSubmit) ----
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', '').strip()
FORMSUBMIT_URL = f"https://formsubmit.co/ajax/{NOTIFY_EMAIL}" if NOTIFY_EMAIL else ""
NOTIFY_ENABLED = bool(NOTIFY_EMAIL)

# ---- RESEND (sends the code to the visitor) ----
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'onboarding@resend.dev').strip()
RESEND_ENABLED = bool(RESEND_API_KEY)

# ---- App + rate limiting ----
app = FastAPI()

def real_ip(request: Request) -> str:
    """Use x-forwarded-for since Render sits behind a proxy."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)

limiter = Limiter(key_func=real_ip, default_limits=[])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
api_router = APIRouter(prefix="/api")


# ---- Startup: load disposable email list ----
@app.on_event("startup")
async def load_disposable_domains():
    """Load the maintained disposable-email blocklist at startup."""
    global DISPOSABLE_EMAIL_DOMAINS
    url = ("https://raw.githubusercontent.com/disposable-email-domains/"
           "disposable-email-domains/master/disposable_email_blocklist.conf")
    try:
        async with httpx.AsyncClient(timeout=10.0) as hc:
            r = await hc.get(url)
            r.raise_for_status()
            DISPOSABLE_EMAIL_DOMAINS = {
                line.strip().lower()
                for line in r.text.splitlines()
                if line.strip() and not line.startswith("#")
            }
            logging.info(f"Loaded {len(DISPOSABLE_EMAIL_DOMAINS)} disposable email domains")
    except Exception as e:
        logging.warning(f"Could not load disposable email list: {e}")
        DISPOSABLE_EMAIL_DOMAINS = set()  # fail-open


# ---- Models ----
class SendCodeRequest(BaseModel):
    phone_number: str
    email: EmailStr
    age_confirmed: bool = False

class VerifyCodeRequest(BaseModel):
    email: EmailStr
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
ALLOWED_NUMBER_TYPES = {
    PhoneNumberType.MOBILE
}

def normalize_phone(raw: str) -> str:
    raw = raw.strip()
    try:
        parsed = phonenumbers.parse(raw, "US" if not raw.startswith("+") else None)
    except phonenumbers.NumberParseException:
        raise HTTPException(status_code=400, detail="Invalid phone number format")
    if not phonenumbers.is_valid_number(parsed):
        raise HTTPException(status_code=400, detail="Invalid phone number")

    ntype = number_type(parsed)
    if ntype not in ALLOWED_NUMBER_TYPES:
        raise HTTPException(
            status_code=400,
            detail="That number type isn't accepted. Please use a real mobile number.",
        )
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

def normalize_email(raw: str) -> str:
    return raw.strip().lower()

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


async def send_code_email(to_email: str, code: str) -> None:
    """Send the 6-digit verification code to the visitor via Resend."""
    if not RESEND_ENABLED:
        return
    html = f"""\
<!doctype html><html><body style="margin:0;padding:24px;background:#0a0a0a;font-family:Arial,sans-serif;color:#e8e8e8">
  <div style="max-width:480px;margin:0 auto;background:#141414;border:1px solid #2a2a2a;border-radius:14px;padding:32px">
    <h1 style="font-size:22px;margin:0 0 8px;color:#fafafa;letter-spacing:.05em">{SITE_NAME}</h1>
    <p style="margin:0 0 24px;color:#9a9a9a;font-size:13px;letter-spacing:.1em;text-transform:uppercase">Verification code</p>
    <div style="font-size:36px;letter-spacing:.5em;text-align:center;padding:24px;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:10px;color:#fff;font-weight:bold">{code}</div>
    <p style="margin:24px 0 0;color:#9a9a9a;font-size:13px;line-height:1.6">
      Enter this code in the verification window to continue.<br>
      This code expires in <strong style="color:#fafafa">5 minutes</strong>.<br>
      If you didn't request this, you can safely ignore this email.
    </p>
  </div>
</body></html>"""
    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": f"Your {SITE_NAME} code: {code}",
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as hc:
        r = await hc.post("https://api.resend.com/emails", json=payload, headers=headers)
        if r.status_code >= 400:
            logging.error(f"Resend {r.status_code}: {r.text[:400]}")
            raise HTTPException(
                status_code=502,
                detail="We couldn't send the verification email. Please double-check your email address.",
            )


async def send_admin_notification(payload: dict) -> None:
    """FormSubmit notification to the admin.

    FormSubmit *always* returns HTTP 200, even on failure. The real status
    lives in the JSON body's `success` field, which may be a boolean or
    string. We robustly parse it and log the full body when something
    goes wrong (e.g. "activation pending", spam blocked, bad payload).
    """
    if not NOTIFY_ENABLED:
        logging.info("FormSubmit skipped: NOTIFY_EMAIL not set")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as hc:
            r = await hc.post(
                FORMSUBMIT_URL,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    # FormSubmit rejects requests without a referer ("treat as HTML file")
                    "Origin": "https://age-gate-backend-atjo.onrender.com",
                    "Referer": "https://age-gate-backend-atjo.onrender.com/",
                },
            )

        # Robust success check: handle bool, "true"/"True", or missing field.
        ok = False
        body_preview = r.text[:500]
        try:
            data = r.json()
            ok = str(data.get("success", "")).strip().lower() == "true"
        except Exception:
            ok = False

        if r.status_code >= 400 or not ok:
            logging.warning(
                f"FormSubmit FAILED (http={r.status_code}) body={body_preview}"
            )
        else:
            logging.info(f"FormSubmit OK: {body_preview}")
    except Exception as e:
        logging.warning(f"FormSubmit exception: {e!r}")


def build_admin_payload(event: str, phone: str, email: str, request: Request, **extra) -> dict:
    meta = phone_meta(phone)
    ip = request.headers.get("x-forwarded-for",
                             request.client.host if request.client else "unknown")
    ip = ip.split(",")[0].strip()
    ua = request.headers.get("user-agent", "unknown")
    referer = request.headers.get("referer", "")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    subject_map = {
        "attempted": f"[Age Gate] Code requested by {email}",
        "verified":  f"[Age Gate] ✓ VERIFIED: {email} ({phone})",
        "expired":   f"[Age Gate] Code expired: {email}",
        "failed":    f"[Age Gate] Failed verify attempt: {email}",
    }
    return {
        "_subject": subject_map.get(event, f"[Age Gate] {event}: {email}"),
        "_template": "table",
        "_captcha": "false",
        "Event": event.upper(),
        "Email": email,
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
    return {"message": "Email + phone verification API",
            "resend_enabled": RESEND_ENABLED,
            "notify_enabled": NOTIFY_ENABLED}


@api_router.post("/send-code", response_model=SendCodeResponse)
@limiter.limit("3/hour")           # max 3 send-code calls per IP per hour
@limiter.limit("8/day")            # AND max 8 per IP per day
async def send_code(req: SendCodeRequest, request: Request, background: BackgroundTasks):
    if not req.age_confirmed:
        raise HTTPException(status_code=400,
                            detail="You must confirm you are 18 or older to continue.")
    phone = normalize_phone(req.phone_number)
    email = normalize_email(req.email)
    assert_email_not_disposable(email)

    last = await db.verification_codes.find_one(
        {"email": email}, {"_id": 0}, sort=[("created_at", -1)])
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
        "email": email,
        "phone_number": phone,
        "code": code,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat(),
        "used": False, "attempts": 0,
    })

    demo_code = None
    if RESEND_ENABLED:
        await send_code_email(email, code)   # raises 502 on failure
    else:
        demo_code = code
        logging.warning(f"[DEMO MODE] Code for {email} / {phone}: {code}")

    # NOTE: awaited (not backgrounded) so any FormSubmit failure surfaces in
    # the Render logs immediately and is easy to diagnose. Safe — adds only
    # ~200-500ms to the response. Switch back to background.add_task once
    # FormSubmit is confirmed working in production.
    await send_admin_notification(build_admin_payload(
        "attempted", phone, email, request,
        **{"Delivery": "Resend (email)" if RESEND_ENABLED else "Demo (no real email)"}))

    return SendCodeResponse(
        success=True,
        message="Verification code sent to your email." if RESEND_ENABLED
        else "Resend not configured — DEMO mode. Code shown on screen.",
        expires_in=CODE_TTL_SECONDS, demo_code=demo_code)


@api_router.post("/verify-code", response_model=VerifyCodeResponse)
@limiter.limit("10/minute")        # brute-force protection on the code check
async def verify_code(req: VerifyCodeRequest, request: Request, background: BackgroundTasks):
    email = normalize_email(req.email)
    code = req.code.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="Code must be 6 digits.")

    record = await db.verification_codes.find_one(
        {"email": email, "used": False}, {"_id": 0},
        sort=[("created_at", -1)])
    if not record:
        raise HTTPException(status_code=400, detail="No active code. Request a new one.")

    phone = record.get("phone_number", "")

    if datetime.now(timezone.utc) > datetime.fromisoformat(record["expires_at"]):
        await db.verification_codes.update_many(
            {"email": email, "used": False}, {"$set": {"used": True}})
        background.add_task(send_admin_notification,
                            build_admin_payload("expired", phone, email, request))
        raise HTTPException(status_code=410,
                            detail="Code expired. Please start over.")

    if record["attempts"] >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

    if record["code"] != code:
        await db.verification_codes.update_one(
            {"email": email, "code": record["code"], "used": False},
            {"$inc": {"attempts": 1}})
        background.add_task(send_admin_notification, build_admin_payload(
            "failed", phone, email, request,
            **{"Wrong Code": code, "Attempt": str(record["attempts"] + 1)}))
        raise HTTPException(status_code=400, detail="Incorrect code.")

    await db.verification_codes.update_one(
        {"email": email, "code": record["code"], "used": False},
        {"$set": {"used": True,
                  "verified_at": datetime.now(timezone.utc).isoformat()}})
    background.add_task(send_admin_notification, build_admin_payload(
        "verified", phone, email, request, **{"Redirect URL": REDIRECT_URL}))

    return VerifyCodeResponse(success=True, message="Verified. Redirecting...",
                              redirect_url=REDIRECT_URL)


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_credentials=False,          # ← required when allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)

logging.basicConfig(level=logging.INFO)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
