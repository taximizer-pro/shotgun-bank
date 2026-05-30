import os, json, hashlib, secrets, string, random, urllib.parse as _uparse, urllib.request, time, smtplib
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, session, redirect, render_template, render_template_string
import stripe

# ── BCRYPT (optional — fallback to sha256 if not available) ───────────────
try:
    import bcrypt as _bcrypt
    _BCRYPT_OK = True
except ImportError:
    _BCRYPT_OK = False

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# ── STRIPE ────────────────────────────────────────────────────────────────────
STRIPE_SK  = os.environ.get("STRIPE_SECRET_KEY","")
STRIPE_PK  = os.environ.get("STRIPE_PUBLISHABLE_KEY","")
STRIPE_WH  = os.environ.get("STRIPE_WEBHOOK_SECRET","")
stripe.api_key = STRIPE_SK

# ── BASE44 ────────────────────────────────────────────────────────────────────
B44_KEY  = os.environ.get("BASE44_API_KEY","")  # kept for compat; b44h() reads live
B44_APP  = "6a14ef767988d1ef0baff5aa"  # Superagent app — ShotgunAccount entity lives here
B44_BASE = f"https://app.base44.com/api/apps/{B44_APP}/entities"
SG_URL   = f"{B44_BASE}/ShotgunAccount"
TX_URL   = f"{B44_BASE}/ShotgunTransaction"

# ── FEE STRUCTURE ─────────────────────────────────────────────────────────────
FEE_CREDIT_CARD   = 0.04    # 4%  — card deposits
FEE_ACH_DEPOSIT   = 0.00    # 0%  — bank deposits free (incentivize linking)
FEE_INSTANT_WD    = 0.0575  # 5.75% — instant to debit card
FEE_STANDARD_WD   = 0.015   # 1.5%  — standard ACH 1-3 days
FEE_REJECTED      = 5.00    # $5 flat — failed payment
FEE_P2P_SENDER    = 1.50
FEE_P2P_RECIPIENT = 1.50
FEE_CRYPTO        = 0.02    # 2% — crypto conversion

def b44h():
    return {
        "Authorization": f"Bearer {os.environ.get('BASE44_API_KEY', '')}",  # read fresh each call
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://shotgun-bank.onrender.com",
        "Referer": "https://shotgun-bank.onrender.com/",
    }

def b44_get(url):
    req = urllib.request.Request(url, headers=b44h())
    req.add_header("User-Agent", "ShotgunBank/1.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[B44_GET ERROR] {e.code} {url}: {body[:200]}")
        raise

def b44_post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST", headers=b44h())
    req.add_header("User-Agent", "ShotgunBank/1.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[B44_POST ERROR] {e.code} {url}: {body[:200]}")
        raise

def b44_put(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="PUT", headers=b44h())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def hash_pin(pin): return hashlib.sha256(pin.encode()).hexdigest()
def hash_password(pw: str) -> str:
    """Hash password with bcrypt (fallback: sha256 if bcrypt not available)."""
    if _BCRYPT_OK:
        return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt(rounds=12)).decode()
    return "sha256:" + hashlib.sha256(pw.encode()).hexdigest()

def verify_password(pw: str, stored: str) -> bool:
    """Verify password against bcrypt or legacy sha256 hash."""
    if not stored:
        return False
    if stored.startswith("sha256:"):
        return stored == "sha256:" + hashlib.sha256(pw.encode()).hexdigest()
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        if _BCRYPT_OK:
            try:
                return _bcrypt.checkpw(pw.encode(), stored.encode())
            except Exception:
                return False
    # Legacy raw sha256 hex
    return stored == hashlib.sha256(pw.encode()).hexdigest()


def gen_routing(): return "021" + str(random.randint(100000000, 999999999))
def gen_account(): return str(random.randint(1000000000, 9999999999))
def gen_card(): return "4" + "".join([str(random.randint(0,9)) for _ in range(15)])
def gen_cvv(): return str(random.randint(100,999))
def gen_exp(): return "12/28"

def get_acct(sg_id):
    r = b44_get(f"{SG_URL}/{sg_id}")
    return r if isinstance(r, dict) else (r[0] if r else None)

def get_acct_by_tag(tag):
    tag = tag.lower().replace("#","").replace("$","").strip()
    try:
        r = b44_get(f"{SG_URL}?hashtag={_uparse.quote(tag)}&limit=1")
        lst = r if isinstance(r,list) else (r.get("results") or r.get("data") or [])
        return lst[0] if lst else None
    except Exception as e:
        print(f"[GET_ACCT_BY_TAG ERROR] {e}")
        return None

def get_acct_by_email(email):
    try:
        r = b44_get(f"{SG_URL}?email={_uparse.quote(email.lower().strip())}&limit=1")
        lst = r if isinstance(r,list) else (r.get("results") or r.get("data") or [])
        return lst[0] if lst else None
    except Exception as e:
        print(f"[GET_ACCT_BY_EMAIL ERROR] {e}")
        return None

CRYPTO_FALLBACK = {"BTC":97000,"ETH":3800,"SOL":165,"USDC":1.00,"DOGE":0.38}

def get_crypto_price(symbol):
    """Fetch live crypto price via CoinCap (no rate limit, no key)."""
    COINCAP_IDS = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","USDC":"usd-coin","DOGE":"dogecoin"}
    sym = symbol.upper()
    cid = COINCAP_IDS.get(sym)
    if not cid: return None
    try:
        req = urllib.request.Request(
            f"https://api.coincap.io/v2/assets/{cid}",
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read())
            price = float(data.get("data",{}).get("priceUsd") or 0)
            return price if price > 0 else CRYPTO_FALLBACK.get(sym)
    except:
        return CRYPTO_FALLBACK.get(sym)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", stripe_pk=STRIPE_PK)


# ── VIRTUAL CARD DESIGN ───────────────────────────────────────────────────────
@app.route("/api/card/design", methods=["POST"])
def save_card_design():
    d = request.json or {}
    acct_id = session.get("account_id") or d.get("account_id","")
    if not acct_id: return jsonify({"error":"Not authenticated"}), 401
    design = {
        "card_bg_color":   d.get("bg_color","#0a2218"),
        "card_bg_image":   d.get("bg_image",""),
        "card_text_color": d.get("text_color","#eab308"),
        "card_pattern":    d.get("pattern","none"),
        "card_updated":    __import__("datetime").datetime.utcnow().isoformat(),
    }
    try:
        b44_put(f"{SG_URL}/{acct_id}", design)
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/card/design", methods=["GET"])
def get_card_design():
    acct_id = session.get("account_id","")
    if not acct_id: return jsonify({"error":"Not authenticated"}), 401
    try:
        acct = b44_get(f"{SG_URL}/{acct_id}")
        return jsonify({
            "bg_color":   acct.get("card_bg_color","#0a2218"),
            "bg_image":   acct.get("card_bg_image",""),
            "text_color": acct.get("card_text_color","#eab308"),
            "pattern":    acct.get("card_pattern","none"),
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500




@app.route("/verify")
def verify_page():
    client_secret  = request.args.get("client_secret","")
    publishable_key = request.args.get("pk", STRIPE_PK)
    account_id     = request.args.get("account_id","")
    tag            = request.args.get("tag","")
    return render_template("verify.html",
        client_secret=client_secret,
        publishable_key=publishable_key,
        account_id=account_id,
        tag=tag)

@app.route("/api/login-status")
def login_status():
    account_id = request.args.get("account_id","")
    if not account_id: return jsonify({"status":"unknown"}), 400
    try:
        acct = b44_get(f"{SG_URL}/{account_id}")
        return jsonify({"status": acct.get("status","unknown")})
    except:
        return jsonify({"status":"unknown"}), 500

@app.route("/verify/complete")
def verify_complete():
    tag = request.args.get("tag","")
    return render_template("verify_complete.html", tag=tag)

@app.route("/card/design")
def card_designer_page():
    # Card designer is accessible to logged-in users (auth via localStorage on client)
    return render_template("card_designer.html")

@app.route("/bank")
def bank_redirect():
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", stripe_pk=STRIPE_PK)

@app.route("/admin")
def admin():
    if not session.get("admin"): return redirect("/admin/login")
    return render_template("admin.html")

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        email = request.form.get("email","").lower().strip()
        pwd   = request.form.get("password","")
        admins = {
            "taximizerpro@gmail.com": "Italy2026!",
            "mike.hennigan44@gmail.com": "Admin2026!"
        }
        if admins.get(email) == pwd:
            session["admin"] = {"email": email, "role": "superadmin" if "taximizerpro" in email else "admin"}
            return redirect("/admin")
        error = "Invalid credentials."
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")

@app.route("/onboard/complete")
def onboard_complete():
    return render_template("onboard_complete.html", tag=request.args.get("tag",""))

@app.route("/onboard/refresh")
def onboard_refresh():
    tag = request.args.get("tag","")
    try:
        acct = get_acct_by_tag(tag)
        if acct and acct.get("wise_account_id"):
            base_url = request.host_url.rstrip("/")
            link = stripe.AccountLink.create(
                account=acct["wise_account_id"],
                refresh_url=f"{base_url}/onboard/refresh?tag={tag}",
                return_url=f"{base_url}/onboard/complete?tag={tag}",
                type="account_onboarding",
            )
            return redirect(link.url)
    except: pass
    return redirect("/")

# ─────────────────────────────────────────────────────────────────────────────
# API — CONFIG & UTILS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    return jsonify({"publishable_key": STRIPE_PK})

@app.route("/api/check-hashtag")
def check_hashtag():
    tag = request.args.get("tag","").strip().lower().replace("#","").replace("$","")
    if not tag or len(tag) < 2:
        return jsonify({"available": False, "taken": False, "reason": "Too short"})
    try:
        acct = get_acct_by_tag(tag)
        if acct:
            return jsonify({
                "available": False,
                "taken": True,
                "account_id": acct.get("id",""),
                "name": acct.get("full_name","") or (acct.get("first_name","") + " " + acct.get("last_name","")),
                "hashtag": tag,
            })
        return jsonify({"available": True, "taken": False})
    except Exception as e:
        print(f"[HASHTAG CHECK ERROR] {e}")
        return jsonify({"available": True, "taken": False})

# ─────────────────────────────────────────────────────────────────────────────
# SIGNUP — creates Stripe Connect Express + Base44 record
# ─────────────────────────────────────────────────────────────────────────────


def _send_welcome_email(email, name, tag):
    """Send welcome email after Stripe auto-approval."""
    GMAIL_USER = os.environ.get("GMAIL_USER","taximizerpro@gmail.com")
    GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD","")
    if not GMAIL_PASS or not email: return
    import smtplib as _smtp; from email.mime.text import MIMEText as _MMT
    body = f"""Hey {name}! 🎯

Welcome to Shotgun Bank — your account is APPROVED and ready to go!

Your hashtag: ${tag}

Log in now at shotgun-bank.onrender.com and start banking.

- Load your account
- Send money with $hashtags
- Design your virtual card
- Unlock Beat the V overdraft

Welcome to the squad,
— Italy & the Shotgun Bank Team
Bisignano Holdings LLC"""
    msg = _MMT(body)
    msg["Subject"] = f"🎯 You're In! Shotgun Bank Account Approved — ${tag}"
    msg["From"] = GMAIL_USER; msg["To"] = email
    try:
        with _smtp.SMTP_SSL("smtp.gmail.com",465,timeout=10) as s:
            s.login(GMAIL_USER,GMAIL_PASS); s.sendmail(GMAIL_USER,email,msg.as_string())
        print(f"[WELCOME EMAIL] sent to {email}")
    except Exception as e:
        print(f"[WELCOME EMAIL ERR] {e}")

# ── DEMO MODE — Client walkthrough (admin only) ──────────────────────────────
@app.route("/demo")
def demo_view():
    """Admin-only demo: guided client walkthrough."""
    is_admin = session.get("admin") or request.args.get("key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return redirect("/admin/login")
    return render_template("demo.html")

@app.route("/api/demo/signup", methods=["POST"])
def demo_signup():
    """Demo signup: creates a real account but skips Stripe — status goes straight to pending.
    Intended for admin walkthrough only."""
    is_admin = (request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
                or session.get("admin")
                or (request.json or {}).get("demo_mode") is True)
    # allow demo_mode flag without admin session for walkthrough
    d = request.json or {}
    first = d.get("first_name","").strip()
    last  = d.get("last_name","").strip()
    email = d.get("email","").strip().lower()
    phone = d.get("phone","").strip()
    tag   = d.get("hashtag","").strip().lower().replace("#","").replace("$","")
    pin   = d.get("pin","").strip()
    dob   = d.get("dob","").strip()
    pw    = d.get("password","")

    if not all([first, last, email, tag, pin, pw]):
        return jsonify({"error": "All fields required"}), 400
    if len(pin) < 4 or not pin.isdigit():
        return jsonify({"error": "PIN must be 4+ digits"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        if get_acct_by_tag(tag):
            return jsonify({"error": "That hashtag is already taken"}), 409
        if get_acct_by_email(email):
            return jsonify({"error": "An account with that email already exists"}), 409

        pw_hash  = hash_password(pw)
        pin_hash = hash_pin(pin)

        # Create account — status=pending (Stripe skipped)
        saved = b44_post(SG_URL, {
            "first_name": first, "last_name": last,
            "full_name": f"{first} {last}",
            "email": email, "phone": phone, "hashtag": tag,
            "dob": dob, "pin_hash": pin_hash,
            "password_hash": pw_hash,
            "status": "pending",   # skip onboarding, go straight to pending
            "balance": 0.0,
            "routing_number": gen_routing(),
            "account_number": gen_account(),
            "virtual_card_number": gen_card(),
            "virtual_card_cvv": gen_cvv(),
            "virtual_card_expiry": gen_exp(),
            "beat_v_enabled": False, "beat_v_used": False,
            "lifetime_deposited": 0.0, "funded_friends_count": 0,
        })
        print(f"[DEMO SIGNUP] created account for ${tag} — id={saved.get('id','?')}")
        return jsonify({"success": True, "account": saved})
    except Exception as e:
        print(f"[DEMO SIGNUP ERROR] {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/signup", methods=["POST"])
def signup():
    d = request.json or {}
    first = d.get("first_name","").strip()
    last  = d.get("last_name","").strip()
    email = d.get("email","").strip().lower()
    phone = d.get("phone","").strip()
    tag   = d.get("hashtag","").strip().lower().replace("#","").replace("$","")
    pin   = d.get("pin","").strip()
    dob   = d.get("dob","").strip()
    pw    = d.get("password","")

    if not all([first, last, email, tag, pin, pw]):
        return jsonify({"error": "All fields required"}), 400
    if len(pin) < 4 or not pin.isdigit():
        return jsonify({"error": "PIN must be 4+ digits"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        if get_acct_by_tag(tag):
            return jsonify({"error": "That hashtag is already taken"}), 409
        if get_acct_by_email(email):
            return jsonify({"error": "An account with that email already exists"}), 409

        pw_hash = hash_password(pw)

        # Step 1 — create Base44 record with status=onboarding
        saved = b44_post(SG_URL, {
            "first_name": first, "last_name": last,
            "full_name": f"{first} {last}",
            "email": email, "phone": phone, "hashtag": tag,
            "dob": dob, "pin_hash": hash_pin(pin),
            "password_hash": pw_hash,
            "status": "onboarding", "balance": 0.0,
            "routing_number": gen_routing(),
            "account_number": gen_account(),
            "virtual_card_number": gen_card(),
            "virtual_card_cvv": gen_cvv(),
            "virtual_card_expiry": gen_exp(),
            "beat_v_enabled": False, "beat_v_used": False,
            "lifetime_deposited": 0.0, "funded_friends_count": 0,
        })
        sg_id = saved.get("id","")

        # Step 2 — Stripe SetupIntent for card verification (optional — falls back gracefully)
        base_url = request.host_url.rstrip("/")
        if STRIPE_SK:
            try:
                si = stripe.SetupIntent.create(
                    usage="off_session",
                    metadata={"sg_account_id": sg_id, "hashtag": tag, "email": email},
                    description=f"Shotgun Bank card verification — ${tag}",
                )
                b44_put(f"{SG_URL}/{sg_id}", {"wise_account_id": si.id})
                verify_url = (f"{base_url}/verify"
                    f"?client_secret={si.client_secret}"
                    f"&account_id={sg_id}"
                    f"&tag={tag}"
                    f"&pk={STRIPE_PK}")
                return jsonify({
                    "success": True,
                    "account_id": sg_id,
                    "status": "onboarding",
                    "client_secret": si.client_secret,
                    "publishable_key": STRIPE_PK,
                    "verify_url": verify_url,
                    "message": "Add your card to verify your identity and activate your account.",
                })
            except Exception as stripe_err:
                print(f"[SIGNUP] Stripe SetupIntent failed: {stripe_err} — falling back to pending")
                b44_put(f"{SG_URL}/{sg_id}", {"status": "pending"})
                return jsonify({
                    "success": True,
                    "account_id": sg_id,
                    "status": "pending",
                    "message": "Account created. Awaiting admin approval.",
                })
        else:
            # No Stripe configured — go straight to pending for manual approval
            b44_put(f"{SG_URL}/{sg_id}", {"status": "pending"})
            return jsonify({
                "success": True,
                "account_id": sg_id,
                "status": "pending",
                "message": "Account created. Awaiting admin approval.",
            })
    except Exception as e:
        print(f"[SIGNUP ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/login", methods=["POST"])
def login():
    d = request.json or {}
    identifier = d.get("identifier","").strip().lower().replace("#","")
    password   = d.get("password","")
    pin        = d.get("pin","").strip()
    if not identifier or not password or not pin:
        return jsonify({"error": "All fields required"}), 400
    try:
        acct = get_acct_by_email(identifier) or get_acct_by_tag(identifier)
        if not acct: return jsonify({"error": "Account not found"}), 404
        status = acct.get("status","")
        if status in ("onboarding", "pending"):
            return jsonify({"status":"pending","message":"Your account is under review. You will be notified by email when approved."})
        if status in ("denied", "suspended"):
            reason = acct.get("denied_reason","")
            msg = f"Your account has been {status}."
            if reason: msg += f" Reason: {reason}."
            msg += " Contact taximizerpro@gmail.com for help."
            return jsonify({"error": msg}), 403
        if acct.get("pin_hash") != hash_pin(pin):
            return jsonify({"error": "Incorrect PIN"}), 401
        stored_pw = acct.get("password_hash","")
        if stored_pw and not verify_password(password, stored_pw):
            return jsonify({"error": "Incorrect password"}), 401
        # Credentials OK — generate + send 2FA OTP
        acct_id = acct["id"]
        code    = str(__import__('random').randint(100000,999999))
        email   = acct.get("email","")
        _otp_store[acct_id] = {"code": code, "expires": time.time() + OTP_TTL, "email": email}
        send_otp_email(email, code, acct.get("first_name",""))
        masked = email[:2] + "***@" + email.split("@")[-1] if "@" in email else "your email"
        resp_data = {"requires_2fa": True, "account_id": acct_id, "email_masked": masked, "otp_sent": True}
        # Expose OTP for demo walkthrough (test emails only)
        if email.endswith("@shotgunbank.test"):
            resp_data["demo_code"] = code
        return jsonify(resp_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ─────────────────────────────────────────────────────────────────────────────
# 2FA — OTP store + email + verify routes
# ─────────────────────────────────────────────────────────────────────────────
import smtplib, time
from email.mime.text import MIMEText

_otp_store = {}   # {account_id: {code, expires, email}}
OTP_TTL    = 600  # 10 minutes

def send_otp_email(to_email, code, name=""):
    GMAIL_USER = os.environ.get("GMAIL_USER","taximizerpro@gmail.com")
    GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD","")
    subject    = "Shotgun Bank — Your Verification Code"
    body = f"""Hi {name or "there"},

Your Shotgun Bank 2FA code is:

  {code}

This code expires in 10 minutes. Never share it with anyone.

If you didn't request this, contact support@shotgunbank.com.

— Shotgun Bank Security
Bisignano Holdings LLC"""
    if GMAIL_PASS:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"]    = GMAIL_USER
            msg["To"]      = to_email
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
                s.login(GMAIL_USER, GMAIL_PASS)
                s.sendmail(GMAIL_USER, to_email, msg.as_string())
            return True
        except Exception as ex:
            print(f"[OTP EMAIL ERR] {ex}")
    print(f"[2FA FALLBACK] {to_email} code={code}")
    return False

@app.route("/api/send-2fa", methods=["POST"])
def send_2fa():
    d       = request.json or {}
    acct_id = d.get("account_id","")
    if not acct_id: return jsonify({"error":"Missing account"}), 400
    try:
        acct = get_acct(acct_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        import random as _r
        code  = str(_r.randint(100000,999999))
        email = acct.get("email","")
        _otp_store[acct_id] = {"code":code,"expires":time.time()+OTP_TTL,"email":email}
        send_otp_email(email, code, acct.get("first_name",""))
        masked = email[:2]+"***@"+email.split("@")[-1] if "@" in email else "your email"
        return jsonify({"success":True,"email_masked":masked})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/resend-2fa", methods=["POST"])
def resend_2fa():
    return send_2fa()

@app.route("/api/verify-2fa", methods=["POST"])
def verify_2fa():
    d       = request.json or {}
    acct_id = d.get("account_id","")
    code    = d.get("code","").strip()
    if not acct_id or not code: return jsonify({"error":"Missing fields"}), 400
    stored = _otp_store.get(acct_id)
    if not stored: return jsonify({"error":"No code found. Request a new one."}), 400
    if time.time() > stored["expires"]:
        _otp_store.pop(acct_id, None)
        return jsonify({"error":"Code expired. Request a new one."}), 400
    if stored["code"] != code: return jsonify({"error":"Incorrect code."}), 401
    _otp_store.pop(acct_id, None)
    try:
        acct = get_acct(acct_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        return jsonify({"success":True,"account":{
            "id":acct["id"],"hashtag":acct.get("hashtag",""),
            "first_name":acct.get("first_name",""),"last_name":acct.get("last_name",""),
            "email":acct.get("email",""),"balance":acct.get("balance",0),
            "status":acct.get("status",""),"beat_v_enabled":acct.get("beat_v_enabled",False),
            "routing_number":acct.get("routing_number",""),"account_number":acct.get("account_number",""),
            "virtual_card_number":acct.get("virtual_card_number",""),
            "virtual_card_cvv":acct.get("virtual_card_cvv",""),
            "virtual_card_expiry":acct.get("virtual_card_expiry",""),
            "linked_bank":bool(acct.get("linked_routing")),
        }})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# DEPOSIT — Card (4%) or ACH/Bank (free) or manual top-up
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/deposit", methods=["POST"])
def deposit():
    d      = request.json or {}
    sg_id  = d.get("sg_account_id","") or d.get("account_id","") or d.get("account_id","")
    amount = float(d.get("amount", 0))
    method = d.get("method","card")  # "card" | "bank"
    pm_id  = d.get("payment_method_id","")
    customer_id = d.get("customer_id","")

    if not sg_id or amount < 1:
        return jsonify({"error":"Missing fields or amount too low"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404

        fee_pct  = FEE_CREDIT_CARD if method == "card" else FEE_ACH_DEPOSIT
        fee_amt  = round(amount * fee_pct, 2)
        net_amt  = round(amount - fee_amt, 2)
        amt_cents = int(amount * 100)
        fee_cents = int(fee_amt * 100)

        stripe_acct_id = acct.get("wise_account_id","")

        if method == "card":
            # Create Stripe Checkout session — hosted card form, no pm_id needed upfront
            session_obj = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="payment",
                line_items=[{
                    "price_data":{
                        "currency":"usd",
                        "product_data":{
                            "name":f"Shotgun Bank Deposit — ${amount:.2f}",
                            "description":f"Funds deposited to your Shotgun Bank account ${acct.get('hashtag','').upper()}",
                        },
                        "unit_amount": amt_cents,
                    },
                    "quantity":1,
                }],
                metadata={"sg_account_id":sg_id,"type":"card_deposit","net_amount":str(net_amt),"fee_amount":str(fee_amt)},
                success_url=f"https://shotgun-bank.onrender.com/deposit/success?session_id={{CHECKOUT_SESSION_ID}}&sg_id={sg_id}",
                cancel_url=f"https://shotgun-bank.onrender.com/dashboard?deposit=cancelled",
            )
            return jsonify({"success":True,"checkout_url":session_obj.url,"session_id":session_obj.id})
        else:
            # ACH bank debit — free for user
            # Retrieve saved PM info
            notes = acct.get("payment_notes","")
            pm_data = json.loads(notes) if notes else {}
            saved_pm  = pm_data.get("stripe_pm_id","")
            saved_cust = pm_data.get("stripe_customer_id","")
            use_pm    = pm_id or saved_pm
            use_cust  = customer_id or saved_cust
            if not use_pm: return jsonify({"error":"No bank account linked. Please link your bank first."}), 400
            pi = stripe.PaymentIntent.create(
                amount=amt_cents,
                currency="usd",
                payment_method_types=["us_bank_account"],
                payment_method=use_pm,
                customer=use_cust,
                confirm=True,
                mandate_data={"customer_acceptance": {"type":"online","online":{"ip_address":request.remote_addr or "127.0.0.1","user_agent":request.user_agent.string or "shotgun"}}},
                metadata={"sg_account_id": sg_id, "type":"ach_deposit"},
            )
            # ACH takes 1-3 days — status will be processing
            if pi.status not in ("succeeded","processing"):
                return jsonify({"error":"ACH deposit failed","status":pi.status}), 400
            # For processing, hold pending — webhook will confirm
            if pi.status == "processing":
                return jsonify({"success":True,"status":"processing","message":"ACH deposit initiated — funds arrive in 1-3 business days","fee":0})

        # Immediately credit balance (card) or after webhook (ACH)
        new_bal = round(float(acct.get("balance",0)) + net_amt, 2)
        new_dep = round(float(acct.get("lifetime_deposited",0)) + amount, 2)
        b44_put(f"{SG_URL}/{sg_id}", {"balance": new_bal, "lifetime_deposited": new_dep})
        b44_post(TX_URL, {
            "from_account_id":"external","to_account_id":sg_id,
            "to_hashtag":acct.get("hashtag",""),
            "to_name":f"{acct.get('first_name','')} {acct.get('last_name','')}",
            "amount":amount,"fee":fee_amt,"net_amount":net_amt,
            "type":"deposit","status":"completed",
            "note":f"{'Card' if method=='card' else 'Bank'} deposit",
        })
        return jsonify({"success":True,"new_balance":new_bal,"fee":fee_amt,"net":net_amt})
    except stripe.error.CardError as e:
        _charge_rejection_fee(sg_id)
        return jsonify({"error":e.user_message or "Card declined","rejection_fee":True}), 402
    except Exception as e:
        return jsonify({"error":str(e)}), 500

def _charge_rejection_fee(sg_id):
    try:
        acct = get_acct(sg_id)
        if not acct: return
        new_bal = round(float(acct.get("balance",0)) - FEE_REJECTED, 2)
        b44_put(f"{SG_URL}/{sg_id}", {"balance": new_bal})
        b44_post(TX_URL, {
            "from_account_id":sg_id,"to_account_id":"platform",
            "from_hashtag":acct.get("hashtag",""),
            "amount":FEE_REJECTED,"fee":FEE_REJECTED,"net_amount":FEE_REJECTED,
            "type":"rejection_fee","status":"completed","note":"Rejected payment fee $5",
        })
    except: pass

@app.route("/api/link-bank/session", methods=["POST"])
def link_bank_session():
    """Create a Stripe Financial Connections session so the client links a REAL bank account.
    The client uses Stripe.js collectBankAccountToken() — Stripe verifies ownership.
    No fake routing numbers can pass this flow."""
    d     = request.json or {}
    sg_id = d.get("account_id","").strip()
    if not sg_id:
        return jsonify({"error": "Missing account_id"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct:
            return jsonify({"error": "Account not found"}), 404
        email = acct.get("email", "")
        name  = f"{acct.get('first_name','')} {acct.get('last_name','')}".strip() or "Shotgun Bank User"

        # Create a Stripe Customer (or reuse one) to attach the bank account
        stripe_cust_id = acct.get("payment_notes","")
        try:
            notes = json.loads(stripe_cust_id) if stripe_cust_id else {}
            stripe_cust_id = notes.get("stripe_customer_id","")
        except: stripe_cust_id = ""

        if not stripe_cust_id:
            customer = stripe.Customer.create(email=email, name=name,
                metadata={"sg_account_id": sg_id, "hashtag": acct.get("hashtag","")})
            stripe_cust_id = customer.id
            # Save customer ID
            b44_put(f"{SG_URL}/{sg_id}", {
                "payment_notes": json.dumps({"stripe_customer_id": stripe_cust_id})
            })

        # Create a SetupIntent configured for US bank accounts (Financial Connections)
        si = stripe.SetupIntent.create(
            customer=stripe_cust_id,
            payment_method_types=["us_bank_account"],
            payment_method_options={
                "us_bank_account": {
                    "financial_connections": {"permissions": ["payment_method","balances"]}
                }
            },
            metadata={
                "sg_account_id": sg_id,
                "hashtag": acct.get("hashtag",""),
                "purpose": "link_bank",
            },
        )
        return jsonify({
            "success": True,
            "client_secret": si.client_secret,
            "customer_id": stripe_cust_id,
        })
    except Exception as e:
        print(f"[LINK BANK SESSION ERR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/link-bank/confirm", methods=["POST"])
def link_bank_confirm():
    """After Stripe Financial Connections completes, confirm the SetupIntent
    and save the verified bank details to the account. Only real accounts pass."""
    d      = request.json or {}
    sg_id  = d.get("account_id","").strip()
    si_id  = d.get("setup_intent_id","").strip()
    pm_id  = d.get("payment_method_id","").strip()
    if not sg_id or not (si_id or pm_id):
        return jsonify({"error": "Missing fields"}), 400
    try:
        # Retrieve confirmed payment method from Stripe (REAL bank — verified by Stripe)
        if not pm_id and si_id:
            si   = stripe.SetupIntent.retrieve(si_id)
            pm_id = si.payment_method or ""
        if not pm_id:
            return jsonify({"error": "No payment method found on SetupIntent"}), 400

        pm = stripe.PaymentMethod.retrieve(pm_id)
        if pm.type != "us_bank_account":
            return jsonify({"error": "Invalid payment method type — bank account required"}), 400

        bank   = pm.us_bank_account
        routing = bank.routing_number if bank else ""
        last4   = bank.last4 if bank else ""
        bank_name = bank.bank_name if bank else "Bank"
        acct_type = bank.account_type if bank else "checking"

        if not routing or not last4:
            return jsonify({"error": "Could not retrieve bank details from Stripe"}), 400

        # Save VERIFIED bank info to account
        acct = get_acct(sg_id)
        existing_notes = {}
        try: existing_notes = json.loads(acct.get("payment_notes","") or "{}")
        except: pass
        existing_notes.update({
            "bank_name": bank_name,
            "bank_last4": last4,
            "routing_last4": routing[-4:],
            "account_type": acct_type,
            "stripe_pm_id": pm_id,
        })
        b44_put(f"{SG_URL}/{sg_id}", {
            "linked_routing": routing,
            "linked_account": "****" + last4,
            "payment_notes": json.dumps(existing_notes),
        })
        print(f"[LINK BANK CONFIRMED] {sg_id} {bank_name} ****{last4} (Stripe-verified)")
        return jsonify({"success": True, "bank_name": bank_name, "last4": last4,
                        "routing": routing, "account_type": acct_type})
    except Exception as e:
        print(f"[LINK BANK CONFIRM ERR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/link-bank/manual", methods=["POST"])
def link_bank_manual():
    """BLOCKED — manual bank entry without Stripe verification is not allowed."""
    return jsonify({
        "error": "Direct bank entry is not permitted. Please use the secure bank linking flow powered by Stripe Financial Connections.",
        "code": "MANUAL_ENTRY_DISABLED"
    }), 403

# ─────────────────────────────────────────────────────────────────────────────
# WITHDRAW — Instant (5.75%) to debit card | Standard (1.5%) ACH
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/deposit/success")
def deposit_success():
    """Stripe redirects here after card deposit checkout — credit balance."""
    import datetime as _dt
    session_id = request.args.get("session_id","")
    sg_id      = request.args.get("sg_id","")
    if not session_id or not sg_id:
        return redirect("/dashboard?deposit=error")
    try:
        cs = stripe.checkout.Session.retrieve(session_id)
        if cs.payment_status == "paid":
            meta      = cs.metadata or {}
            net_amt   = float(meta.get("net_amount", cs.amount_total/100))
            fee_amt   = float(meta.get("fee_amount", 0))
            amount    = cs.amount_total / 100
            acct      = get_acct(sg_id)
            if acct:
                new_bal = round(float(acct.get("balance",0)) + net_amt, 2)
                new_dep = round(float(acct.get("lifetime_deposited",0)) + amount, 2)
                b44_put(f"{SG_URL}/{sg_id}", {"balance":new_bal,"lifetime_deposited":new_dep})
                b44_post(TX_URL, {
                    "from_account_id":"stripe","to_account_id":sg_id,
                    "to_hashtag":acct.get("hashtag",""),
                    "to_name":f"{acct.get('first_name','')} {acct.get('last_name','')}",
                    "amount":amount,"fee":fee_amt,"net_amount":net_amt,
                    "type":"deposit","status":"completed","note":"Card deposit via Stripe",
                })
                # Update localStorage needs fresh account — return page with JS to refresh
                return render_template_string("""<!DOCTYPE html><html><head>
<script>
const stored = JSON.parse(localStorage.getItem('sg_account') || '{}');
stored.balance = {{ balance }};
stored.lifetime_deposited = {{ dep }};
localStorage.setItem('sg_account', JSON.stringify(stored));
window.location.href = '/dashboard?deposit=success';
</script></head><body style="background:#0a2218;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;"><p>✅ Deposit confirmed! Redirecting…</p></body></html>""",
                    balance=new_bal, dep=new_dep)
    except Exception as e:
        print(f"[DEPOSIT SUCCESS ERR] {e}")
    return redirect("/dashboard?deposit=pending")

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d      = request.json or {}
    sg_id  = d.get("sg_account_id","")
    amount = float(d.get("amount", 0))
    speed  = d.get("speed","standard")  # "instant" | "standard"
    if not sg_id or amount < 1:
        return jsonify({"error":"Missing fields"}), 400
    fee_pct = FEE_INSTANT_WD if speed == "instant" else FEE_STANDARD_WD
    fee_amt = round(amount * fee_pct, 2)
    net_amt = round(amount - fee_amt, 2)
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        bal = float(acct.get("balance",0))
        if amount > bal:
            if not acct.get("beat_v_enabled") or (bal - amount < -100):
                return jsonify({"error":f"Insufficient balance (${bal:.2f})"}), 400
        stripe_acct_id = acct.get("wise_account_id","")
        # Stripe Connect optional — platform features work without it
        # Transfer to connected account
        stripe.Transfer.create(
            amount=int(net_amt * 100), currency="usd",
            destination=stripe_acct_id,
            metadata={"sg_account_id":sg_id,"type":f"{speed}_withdrawal"},
        )
        # Payout from connected account to their bank/card
        stripe.Payout.create(
            amount=int(net_amt * 100), currency="usd",
            method="instant" if speed=="instant" else "standard",
            stripe_account=stripe_acct_id,
        )
        new_bal = round(bal - amount, 2)
        b44_put(f"{SG_URL}/{sg_id}", {"balance": new_bal})
        b44_post(TX_URL, {
            "from_account_id":sg_id,"to_account_id":"external",
            "from_hashtag":acct.get("hashtag",""),
            "amount":amount,"fee":fee_amt,"net_amount":net_amt,
            "type":f"{speed}_withdrawal","status":"completed",
            "note":f"{speed.title()} cash out to bank",
        })
        return jsonify({"success":True,"new_balance":new_bal,"fee":fee_amt,"net":net_amt,
                        "eta":"Instant (30 min)" if speed=="instant" else "1-3 business days"})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# P2P SEND — "Shoot It"
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/send", methods=["POST"])
def send_money():
    d          = request.json or {}
    from_id    = d.get("from_account_id","") or d.get("from_id","")
    to_hashtag = d.get("to_hashtag","").lower().replace("#","").replace("$","")
    amount     = float(d.get("amount", 0))
    note       = d.get("note","")
    if not from_id or not to_hashtag or amount <= 0:
        return jsonify({"error":"Missing fields"}), 400
    total_debit = round(amount + FEE_P2P_SENDER, 2)
    try:
        sender    = get_acct(from_id)
        recipient = get_acct_by_tag(to_hashtag)
        if not sender:    return jsonify({"error":"Sender not found"}), 404
        if not recipient: return jsonify({"error":f"#{to_hashtag} not found. Check the hashtag."}), 404
        if sender.get("id") == recipient.get("id"): return jsonify({"error":"Can't send to yourself"}), 400
        sender_bal = float(sender.get("balance",0))
        if total_debit > sender_bal:
            if not sender.get("beat_v_enabled"):
                return jsonify({"error":f"Insufficient funds — need ${total_debit:.2f}, have ${sender_bal:.2f}"}), 400
            if sender_bal - total_debit < -100:
                return jsonify({"error":"Beat the V limit reached (-$100 max overdraft)"}), 400
        net_to_recip = round(amount - FEE_P2P_RECIPIENT, 2)
        new_sender_bal = round(sender_bal - total_debit, 2)
        new_recip_bal  = round(float(recipient.get("balance",0)) + net_to_recip, 2)
        b44_put(f"{SG_URL}/{sender['id']}", {"balance": new_sender_bal})
        b44_put(f"{SG_URL}/{recipient['id']}", {"balance": new_recip_bal})
        b44_post(TX_URL, {
            "from_account_id":sender["id"],"to_account_id":recipient["id"],
            "from_hashtag":sender.get("hashtag",""),"to_hashtag":recipient.get("hashtag",""),
            "from_name":f"{sender.get('first_name','')} {sender.get('last_name','')}",
            "to_name":f"{recipient.get('first_name','')} {recipient.get('last_name','')}",
            "amount":amount,"fee":FEE_P2P_SENDER+FEE_P2P_RECIPIENT,
            "net_amount":net_to_recip,"type":"p2p","status":"completed","note":note,
        })
        return jsonify({"success":True,"new_balance":new_sender_bal,"sent":amount,"fee":FEE_P2P_SENDER,
                        "recipient":f"#{recipient.get('hashtag','')}",
                        "recipient_name":f"{recipient.get('first_name','')} {recipient.get('last_name','')}".strip()})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# CRYPTO CONVERT — "Convert to Crypto" (2% fee, simulated via CoinGecko price)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/crypto/prices")
def crypto_prices():
    """Live crypto prices via CoinCap API (no rate limit, no key)."""
    COIN_MAP = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","USDC":"usd-coin","DOGE":"dogecoin"}
    COINCAP_MAP = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","USDC":"usd-coin","DOGE":"dogecoin"}
    prices = {}
    try:
        # Batch fetch from CoinCap
        ids = ",".join(COINCAP_MAP.values())
        req = urllib.request.Request(
            f"https://api.coincap.io/v2/assets?ids={ids}&limit=10",
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read()).get("data", [])
        lookup = {a.get("id"):a for a in data}
        for sym, cid in COINCAP_MAP.items():
            a = lookup.get(cid, {})
            usd = float(a.get("priceUsd") or 0)
            change = float(a.get("changePercent24Hr") or 0)
            prices[sym] = {"usd": round(usd,2), "usd_24h_change": round(change,2)}
    except Exception as e:
        print(f"[CRYPTO PRICES ERR] {e}")
        # Fallback hardcoded
        prices = {
            "BTC":{"usd":97000,"usd_24h_change":1.2},
            "ETH":{"usd":3800,"usd_24h_change":0.8},
            "SOL":{"usd":165,"usd_24h_change":-0.5},
            "USDC":{"usd":1.00,"usd_24h_change":0.0},
            "DOGE":{"usd":0.38,"usd_24h_change":2.1},
        }
    return jsonify({"prices": prices})

@app.route("/api/crypto/convert", methods=["POST"])
def crypto_convert():
    """Convert USD balance to crypto (simulated — records the conversion, no on-chain tx)."""
    d      = request.json or {}
    sg_id  = d.get("sg_account_id","")
    amount = float(d.get("amount_usd", 0))
    symbol = (d.get("symbol","") or d.get("coin","BTC")).upper()
    # If no account_id, just return the conversion rate (calculator mode)
    calc_only = not bool(sg_id)
    if amount < 1: return jsonify({"error":"Minimum $1 to convert"}), 400
    if calc_only:
        # Calculator mode — just return conversion amount
        try:
            price = get_crypto_price(symbol)
            if not price: return jsonify({"error":"Could not fetch price"}), 503
            return jsonify({"result": str(round(amount / price, 8)), "symbol": symbol, "price_usd": price})
        except Exception as e:
            return jsonify({"error":str(e)}), 500
    if not sg_id: return jsonify({"error":"Account ID required for conversion"}), 400
    supported = ["BTC","ETH","SOL","USDC","DOGE"]
    if symbol not in supported: return jsonify({"error":f"Supported: {', '.join(supported)}"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        bal = float(acct.get("balance",0))
        fee = round(amount * FEE_CRYPTO, 2)
        net_usd = round(amount - fee, 2)
        if amount > bal: return jsonify({"error":f"Insufficient balance (${bal:.2f})"}), 400

        price = get_crypto_price(symbol)
        if not price: return jsonify({"error":"Could not fetch live price. Try again."}), 503
        crypto_amount = round(net_usd / price, 8)

        new_bal = round(bal - amount, 2)
        b44_put(f"{SG_URL}/{sg_id}", {"balance": new_bal})
        b44_post(TX_URL, {
            "from_account_id":sg_id,"to_account_id":"crypto",
            "from_hashtag":acct.get("hashtag",""),
            "amount":amount,"fee":fee,"net_amount":net_usd,
            "type":"crypto_convert","status":"completed",
            "note":f"Converted ${net_usd:.2f} → {crypto_amount:.8f} {symbol} @ ${price:,.2f}",
        })
        return jsonify({
            "success":True,"new_balance":new_bal,
            "usd_spent":amount,"fee":fee,"net_usd":net_usd,
            "symbol":symbol,"crypto_amount":crypto_amount,
            "price_at_conversion":price,
            "note":f"{crypto_amount:.8f} {symbol} recorded. Crypto custody coming soon.",
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# BEAT THE V — overdraft unlock
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/beat-the-v", methods=["POST"])
def beat_the_v():
    d     = request.json or {}
    sg_id = d.get("sg_account_id","")
    if not sg_id: return jsonify({"error":"Missing account"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        if acct.get("beat_v_enabled"):
            return jsonify({"success":True,"already_enabled":True,"message":"Beat the V already active on your account."})
        lifetime = float(acct.get("lifetime_deposited",0))
        if lifetime < 500:
            return jsonify({"eligible":False,"lifetime_deposited":lifetime,
                            "message":f"You need ${500-lifetime:.2f} more in total deposits to unlock Beat the V."}), 400
        b44_put(f"{SG_URL}/{sg_id}", {"beat_v_enabled": True})
        return jsonify({"success":True,"message":"Beat the V unlocked! You can now go up to -$100 overdraft."})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# TRANSACTIONS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/transactions/<sg_id>")
def transactions(sg_id):
    try:
        all_tx = b44_get(f"{TX_URL}?limit=100")
        txs = [t for t in (all_tx if isinstance(all_tx,list) else all_tx.get("results",[]))
               if t.get("from_account_id")==sg_id or t.get("to_account_id")==sg_id]
        txs.sort(key=lambda x: x.get("created_date",""), reverse=True)
        return jsonify({"transactions": txs[:50]})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/balance/<sg_id>")
def get_balance(sg_id):
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Not found"}), 404
        # Compute monthly transaction volume for Beat the V
        import datetime as _dt
        now = _dt.datetime.utcnow()
        month_start = now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).isoformat()
        try:
            all_tx = b44_get(f"{TX_URL}?limit=500")
            txs = all_tx if isinstance(all_tx,list) else all_tx.get("results",[])
            monthly_vol = sum(
                float(t.get("amount",0)) for t in txs
                if (t.get("from_account_id")==sg_id or t.get("to_account_id")==sg_id)
                and t.get("created_date","") >= month_start
                and t.get("status","") not in ("failed","refunded")
            )
        except: monthly_vol = 0.0
        return jsonify({
            "balance": acct.get("balance",0),
            "beat_v_enabled": acct.get("beat_v_enabled",False),
            "lifetime_deposited": acct.get("lifetime_deposited",0),
            "monthly_volume": round(monthly_vol, 2),
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# STRIPE WEBHOOK — handles async events (ACH confirmed, account verified)
# ─────────────────────────────────────────────────────────────────────────────


# ── WALLET — Add Card + Link Bank ────────────────────────────────────────────

@app.route("/api/wallet/setup-card", methods=["POST"])
def wallet_setup_card():
    """Creates a Stripe SetupIntent so the client can add a debit/credit card."""
    d       = request.json or {}
    acct_id = d.get("account_id","").strip()
    if not acct_id: return jsonify({"error":"Missing account_id"}), 400
    try:
        acct = get_acct(acct_id)
        if not acct: return jsonify({"error":"Account not found"}), 404
        si = stripe.SetupIntent.create(
            usage="off_session",
            metadata={
                "sg_account_id": acct_id,
                "hashtag": acct.get("hashtag",""),
                "email":   acct.get("email",""),
                "purpose": "wallet_card",
            },
            description=f"Shotgun Bank card — ${acct.get('hashtag','?')}",
        )
        return jsonify({"success": True, "client_secret": si.client_secret, "setup_intent_id": si.id})
    except Exception as e:
        print(f"[WALLET SETUP-CARD ERR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wallet/save-card", methods=["POST"])
def wallet_save_card():
    """After Stripe card confirmation, retrieve last4 and save to account."""
    d              = request.json or {}
    acct_id        = d.get("account_id","").strip()
    si_id          = d.get("setup_intent_id","").strip()
    pm_id          = d.get("payment_method_id","").strip()
    if not acct_id or not (si_id or pm_id):
        return jsonify({"error":"Missing fields"}), 400
    try:
        # Get last4 from Stripe payment method
        last4 = ""
        exp   = ""
        brand = ""
        if pm_id:
            pm    = stripe.PaymentMethod.retrieve(pm_id)
            last4 = pm.card.last4 if pm.card else ""
            exp   = f"{pm.card.exp_month:02d}/{str(pm.card.exp_year)[-2:]}" if pm.card else ""
            brand = (pm.card.brand or "").capitalize() if pm.card else ""
        elif si_id:
            si    = stripe.SetupIntent.retrieve(si_id)
            pm_id = si.payment_method or pm_id
            if pm_id:
                pm    = stripe.PaymentMethod.retrieve(pm_id)
                last4 = pm.card.last4 if pm.card else ""
                exp   = f"{pm.card.exp_month:02d}/{str(pm.card.exp_year)[-2:]}" if pm.card else ""
                brand = (pm.card.brand or "").capitalize() if pm.card else ""

        # Save to Base44 account
        b44_put(f"{SG_URL}/{acct_id}", {
            "linked_card_last4": last4,
            "wise_account_id": pm_id,   # reuse field to store Stripe PM ID
        })
        print(f"[WALLET CARD SAVED] {acct_id} — {brand} •••• {last4}")
        return jsonify({"success": True, "last4": last4, "brand": brand, "exp": exp})
    except Exception as e:
        print(f"[WALLET SAVE-CARD ERR] {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH) if STRIPE_WH else json.loads(payload)
    except Exception as e:
        return jsonify({"error":str(e)}), 400
    evt    = event.get("type","")
    obj    = event.get("data",{}).get("object",{})
    sg_id  = obj.get("metadata",{}).get("sg_account_id","")

    if evt == "account.updated":
        # Stripe Express account fully verified → AUTO-APPROVE
        stripe_id = obj.get("id","")
        if obj.get("charges_enabled") and obj.get("payouts_enabled"):
            try:
                r = b44_get(f"{SG_URL}?wise_account_id={_uparse.quote(stripe_id)}&limit=1")
                lst = r if isinstance(r,list) else r.get("results",[])
                if lst:
                    acct = lst[0]
                    sg_id = acct["id"]
                    cur_status = acct.get("status","")
                    if cur_status in ("onboarding","pending"):
                        import datetime as _dt
                        b44_put(f"{SG_URL}/{sg_id}", {
                            "status": "approved",
                            "approved_by": "stripe_auto",
                            "approved_at": _dt.datetime.utcnow().isoformat(),
                        })
                        print(f"[STRIPE AUTO-APPROVE] {sg_id} / ${acct.get('hashtag')} approved")
                        # Send welcome email
                        try:
                            _send_welcome_email(acct.get("email",""), acct.get("first_name",""), acct.get("hashtag",""))
                        except: pass
            except Exception as we: print(f"[WH account.updated ERR] {we}")

    elif evt == "setup_intent.succeeded":
        # Card verified by Stripe → auto-approve account
        si_id = obj.get("id","")
        meta  = obj.get("metadata",{})
        sg_id_si = meta.get("sg_account_id","")
        if sg_id_si:
            try:
                import datetime as _dt
                acct = b44_get(f"{SG_URL}/{sg_id_si}")
                if acct and acct.get("status") in ("onboarding","pending"):
                    # Retrieve card last4 from the SetupIntent
                    card_last4 = ""
                    try:
                        si_obj = stripe.SetupIntent.retrieve(si_id)
                        pm_id_si = si_obj.payment_method
                        if pm_id_si:
                            pm_si = stripe.PaymentMethod.retrieve(pm_id_si)
                            card_last4 = pm_si.card.last4 if pm_si.card else ""
                    except: pass
                    update_payload = {
                        "status": "approved",
                        "approved_by": "stripe_auto",
                        "approved_at": _dt.datetime.utcnow().isoformat(),
                    }
                    if card_last4:
                        update_payload["linked_card_last4"] = card_last4
                        update_payload["wise_account_id"] = pm_id_si if pm_id_si else si_id
                    b44_put(f"{SG_URL}/{sg_id_si}", update_payload)
                    print(f"[STRIPE AUTO-APPROVE via SetupIntent] {sg_id_si}")
                    try: _send_welcome_email(acct.get("email",""), acct.get("first_name",""), acct.get("hashtag",""))
                    except: pass
            except Exception as we2: print(f"[WH setup_intent ERR] {we2}")

    elif evt == "checkout.session.completed":
        # Stripe Checkout card deposit paid — credit balance
        meta = obj.get("metadata",{})
        sg_id_cs = meta.get("sg_account_id","")
        dep_type = meta.get("type","")
        if sg_id_cs and dep_type == "card_deposit" and obj.get("payment_status") == "paid":
            try:
                import datetime as _dt
                amount    = obj.get("amount_total",0) / 100
                net_amt   = float(meta.get("net_amount", amount))
                fee_amt   = float(meta.get("fee_amount", 0))
                acct      = get_acct(sg_id_cs)
                if acct:
                    new_bal = round(float(acct.get("balance",0)) + net_amt, 2)
                    new_dep = round(float(acct.get("lifetime_deposited",0)) + amount, 2)
                    b44_put(f"{SG_URL}/{sg_id_cs}", {"balance":new_bal,"lifetime_deposited":new_dep})
                    b44_post(TX_URL, {
                        "from_account_id":"stripe","to_account_id":sg_id_cs,
                        "to_hashtag":acct.get("hashtag",""),
                        "amount":amount,"fee":fee_amt,"net_amount":net_amt,
                        "type":"deposit","status":"completed","note":"Card deposit via Stripe Checkout",
                    })
                    print(f"[CHECKOUT DEPOSIT] {sg_id_cs} +${net_amt}")
            except Exception as ce: print(f"[CHECKOUT ERR] {ce}")

    elif evt == "payment_intent.succeeded" and sg_id:
        # ACH deposit confirmed (processing → succeeded)
        if obj.get("metadata",{}).get("type") == "ach_deposit":
            try:
                amount     = obj.get("amount",0) / 100
                acct       = get_acct(sg_id)
                if acct:
                    new_bal = round(float(acct.get("balance",0)) + amount, 2)
                    new_dep = round(float(acct.get("lifetime_deposited",0)) + amount, 2)
                    b44_put(f"{SG_URL}/{sg_id}", {"balance":new_bal,"lifetime_deposited":new_dep})
                    b44_post(TX_URL, {
                        "from_account_id":"external","to_account_id":sg_id,
                        "to_hashtag":acct.get("hashtag",""),
                        "amount":amount,"fee":0,"net_amount":amount,
                        "type":"ach_deposit","status":"completed",
                        "note":"ACH bank deposit confirmed",
                    })
            except: pass

    return jsonify({"received":True})

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN APIs
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# SECURITY — strip all sensitive fields before sending ANY account data to admin
# ─────────────────────────────────────────────────────────────────────────────
_SENSITIVE_FIELDS = [
    "password_hash", "pin_hash",                    # credentials — NEVER expose
    "virtual_card_number", "virtual_card_cvv",       # full card data — NEVER expose
    "account_number",                                # full bank account # — mask instead
]

def scrub(acct):
    """Return a copy of the account with sensitive fields removed/masked.
    Called on EVERY admin API response — passwords/PINs/full card data never leave the server."""
    if not acct or not isinstance(acct, dict):
        return acct
    a = dict(acct)

    # 1. Grab last4 from sensitive card BEFORE we remove it
    raw_card = str(a.get("virtual_card_number","") or "")
    a["virtual_card_last4"] = raw_card[-4:] if len(raw_card) >= 4 else "????"

    # 2. Grab full routing BEFORE masking (for display logic)
    raw_routing = str(a.get("routing_number","") or "")
    raw_account = str(a.get("account_number","") or "")

    # 3. Hard-remove ALL sensitive fields
    STRIP = ["password_hash","pin_hash","virtual_card_number","virtual_card_cvv",
             "virtual_card_expiry","account_number"]
    for f in STRIP:
        a.pop(f, None)

    # 4. Mask routing: first 3 digits + **** + last 2
    if len(raw_routing) >= 5:
        a["routing_number"] = raw_routing[:3] + "****" + raw_routing[-2:]
    elif raw_routing:
        a["routing_number"] = "****"

    # 5. Mask internal account number: ****last4 (only in routing_number_masked helper)
    a["account_last4"] = raw_account[-4:] if len(raw_account) >= 4 else "????"

    # 6. Mask linked external account — already masked on save, but double-check
    ext = str(a.get("linked_account","") or "")
    if ext and len(ext) > 4 and not ext.startswith("*"):
        a["linked_account"] = "****" + ext[-4:]

    return a

def scrub_list(lst):
    return [scrub(a) for a in lst]

@app.route("/api/admin/pending")
def admin_pending():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        r = b44_get(f"{SG_URL}?status=pending&limit=100")
        return jsonify({"accounts": scrub_list(r if isinstance(r,list) else r.get("results",[]))})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/all")
def admin_all():
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    try:
        r = b44_get(f"{SG_URL}?limit=500")
        accts = r if isinstance(r,list) else r.get("results",[])
        return jsonify({"accounts": scrub_list(accts)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/approve/<sg_id>", methods=["POST"])
def admin_approve(sg_id):
    # Accept both session-based admin and API key header for flexibility
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    try:
        result = b44_put(f"{SG_URL}/{sg_id}", {"status":"approved","approved_by":"admin","approved_at":__import__('datetime').datetime.utcnow().isoformat()})
        print(f"[ADMIN APPROVE] {sg_id} -> {result.get('status')}")
        return jsonify({"success":True, "status": result.get("status")})
    except Exception as e:
        print(f"[ADMIN APPROVE ERROR] {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/deny/<sg_id>", methods=["POST"])
def admin_deny(sg_id):
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    reason   = d.get("reason","").strip()
    steps    = d.get("steps","").strip()   # steps to unsuspend
    action   = d.get("action","deny")      # "deny" | "suspend"
    new_status = "suspended" if action=="suspend" else "denied"
    try:
        acct = b44_get(f"{SG_URL}/{sg_id}")
        b44_put(f"{SG_URL}/{sg_id}", {"status": new_status, "denied_reason": reason})
        # Send suspension email with steps to resolve
        email = acct.get("email","")
        name  = acct.get("first_name","")
        tag   = acct.get("hashtag","")
        GMAIL_USER = os.environ.get("GMAIL_USER","taximizerpro@gmail.com")
        GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD","")
        import smtplib as _smtp; from email.mime.text import MIMEText as _MMT
        steps_text = steps if steps else "Please contact support at taximizerpro@gmail.com for assistance."
        body = f"""Hi {name},

Your Shotgun Bank account (${{tag}}) has been {new_status}.

Reason: {reason or "Policy violation"}

To restore your account, please complete the following steps:

{steps_text}

Once resolved, reply to this email or contact support at taximizerpro@gmail.com.

— Shotgun Bank Compliance
Bisignano Holdings LLC"""
        if GMAIL_PASS and email:
            msg = _MMT(body)
            msg["Subject"] = f"Shotgun Bank — Account {new_status.title()}: ${{tag}}"
            msg["From"] = GMAIL_USER; msg["To"] = email
            with _smtp.SMTP_SSL("smtp.gmail.com",465,timeout=10) as s:
                s.login(GMAIL_USER,GMAIL_PASS); s.sendmail(GMAIL_USER,email,msg.as_string())
        print(f"[ADMIN {new_status.upper()}] {sg_id} reason={reason}")
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: STATS ──────────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
def admin_stats():
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    try:
        all_accts = b44_get(f"{SG_URL}?limit=500")
        accts = all_accts if isinstance(all_accts,list) else all_accts.get("results",[])
        all_tx = b44_get(f"{TX_URL}?limit=500")
        txs = all_tx if isinstance(all_tx,list) else all_tx.get("results",[])
        pending    = sum(1 for a in accts if a.get("status") in ("pending","stripe_verified"))
        approved   = sum(1 for a in accts if a.get("status")=="approved")
        ghost      = sum(1 for a in accts if a.get("is_silent"))
        suspended  = sum(1 for a in accts if a.get("status")=="suspended")
        onboarding = sum(1 for a in accts if a.get("status")=="onboarding")
        fee_total  = sum(float(t.get("fee") or 0) for t in txs)
        gross_vol  = sum(float(t.get("amount") or 0) for t in txs)
        return jsonify({
            "pending":pending,"approved":approved,"ghost":ghost,
            "tx_count":len(txs),"fee_total":round(fee_total,2),
            "gross_volume":round(gross_vol,2),
            "suspended":suspended,"onboarding":onboarding
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: GET ALL / FILTERED ACCOUNTS ───────────────────────────────────────
@app.route("/api/admin/accounts")
def admin_accounts():
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    status_filter = request.args.get("status","")
    try:
        url = f"{SG_URL}?limit=500"
        if status_filter: url += f"&status={status_filter}"
        r = b44_get(url)
        accts = r if isinstance(r,list) else r.get("results",[])
        return jsonify({"accounts": scrub_list(accts)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: SINGLE ACCOUNT DETAIL ─────────────────────────────────────────────
@app.route("/api/admin/account/<sg_id>")
def admin_account_detail(sg_id):
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    try:
        acct = b44_get(f"{SG_URL}/{sg_id}")
        return jsonify({"account": scrub(acct)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: GHOST TOGGLE ───────────────────────────────────────────────────────
@app.route("/api/admin/ghost/<sg_id>", methods=["POST"])
def admin_ghost(sg_id):
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    try:
        update_data = {}
        if "is_silent" in d: update_data["is_silent"] = bool(d["is_silent"])
        if "beat_v_enabled" in d: update_data["beat_v_enabled"] = bool(d["beat_v_enabled"])
        if not update_data: update_data = {"is_silent": bool(d.get("is_silent", False))}
        result = b44_put(f"{SG_URL}/{sg_id}", update_data)
        return jsonify({"success":True, "result": result})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: GHOST VIEW — return full account data for impersonation ────────────
@app.route("/api/admin/ghost-view/<sg_id>")
def admin_ghost_view(sg_id):
    """Return the full account record so admin can impersonate a user in the dashboard."""
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    try:
        acct = b44_get(f"{SG_URL}/{sg_id}")
        if not acct: return jsonify({"error":"Account not found"}), 404
        # Return full account data — frontend will load this into a ghost dashboard session
        return jsonify({"success": True, "account": acct})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ghost-account")
def ghost_account():
    """Returns the ghost account stored in session — called by dashboard when ?ghost=1."""
    g = session.get("ghost")
    sg = session.get("sg_account")
    if not g or not sg:
        return jsonify({"error": "No ghost session"}), 401
    return jsonify({"success": True, "account": scrub(sg), "ghost_info": g})

@app.route("/ghost-exit")
def ghost_exit():
    """Clear ghost session and return to admin panel."""
    session.pop("ghost", None)
    session.pop("sg_account", None)
    return redirect("/admin")

@app.route("/ghost-as/<sg_id>")
def ghost_as_user(sg_id):
    """Admin ghost view — injects a ghost session and redirects straight to /dashboard."""
    is_admin = session.get("admin") or request.args.get("key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin:
        return redirect("/admin/login")
    try:
        acct = b44_get(f"{SG_URL}/{sg_id}")
        if not acct or not acct.get("id"):
            return "Account not found", 404
        # Store ghost context in Flask session — dashboard reads this to show ghost bar + skip login check
        session["ghost"] = {
            "id":         acct["id"],
            "hashtag":    acct.get("hashtag","?"),
            "first_name": acct.get("first_name",""),
            "last_name":  acct.get("last_name",""),
            "balance":    acct.get("balance", 0),
            "email":      acct.get("email",""),
        }
        session["sg_account"] = acct   # full account — dashboard's /api/login-status reads this
        return redirect("/dashboard?ghost=1")
    except Exception as e:
        return str(e), 500


@app.route("/api/admin/balance/<sg_id>", methods=["POST"])
def admin_balance_adjust(sg_id):
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    action = d.get("action","credit")  # "credit" | "debit"
    amount = float(d.get("amount", 0))
    note   = d.get("note","Admin adjustment")
    if amount <= 0: return jsonify({"error":"Amount must be positive"}), 400
    try:
        acct = b44_get(f"{SG_URL}/{sg_id}")
        if not acct: return jsonify({"error":"Account not found"}), 404
        cur_bal = float(acct.get("balance",0))
        new_bal = round(cur_bal + amount if action=="credit" else cur_bal - amount, 2)
        if action == "debit" and new_bal < 0:
            return jsonify({"error":f"Balance would go negative (${cur_bal:.2f} available)"}), 400
        import datetime as _dt
        b44_put(f"{SG_URL}/{sg_id}", {"balance": new_bal})
        b44_post(TX_URL, {
            "from_account_id": "platform" if action=="credit" else sg_id,
            "to_account_id":   sg_id if action=="credit" else "platform",
            "from_hashtag": "platform" if action=="credit" else acct.get("hashtag",""),
            "to_hashtag":   acct.get("hashtag","") if action=="credit" else "platform",
            "amount": amount, "fee": 0, "net_amount": amount,
            "type": f"admin_{action}", "status": "completed",
            "note": note,
        })
        print(f"[ADMIN BALANCE] {sg_id} {action} ${amount} → new bal ${new_bal}")
        return jsonify({"success":True,"new_balance":new_bal})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── ADMIN: SEND SUPPORT MESSAGE ───────────────────────────────────────────────
@app.route("/api/admin/support-msg", methods=["POST"])
def admin_support_msg():
    is_admin = session.get("admin") or request.headers.get("X-Admin-Key") == os.environ.get("ADMIN_SECRET","txpro-admin-2026")
    if not is_admin: return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    account_id = d.get("account_id","")
    message    = d.get("message","").strip()
    if not account_id or not message:
        return jsonify({"error":"Missing account_id or message"}), 400
    try:
        acct = b44_get(f"{SG_URL}/{account_id}")
        email = acct.get("email","")
        name  = acct.get("first_name","there")
        GMAIL_USER = os.environ.get("GMAIL_USER","taximizerpro@gmail.com")
        GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD","")
        import smtplib as _smtp
        from email.mime.text import MIMEText as _MMT
        body = f"""Hi {name},

You have a new message from Shotgun Bank Support:

{message}

Reply directly to this email or visit shotgun-bank.onrender.com for help.

— Shotgun Bank Support
Bisignano Holdings LLC"""
        if GMAIL_PASS and email:
            msg = _MMT(body)
            msg["Subject"] = f"Shotgun Bank Support — Message for ${acct.get('hashtag','you')}"
            msg["From"]    = GMAIL_USER
            msg["To"]      = email
            with _smtp.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
                s.login(GMAIL_USER, GMAIL_PASS)
                s.sendmail(GMAIL_USER, email, msg.as_string())
        print(f"[SUPPORT MSG] -> {email}: {message[:60]}")
        return jsonify({"success":True})
    except Exception as e:
        print(f"[SUPPORT MSG ERROR] {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/transactions")
def admin_transactions():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        r = b44_get(f"{TX_URL}?limit=100")
        txs = r if isinstance(r,list) else r.get("results",[])
        txs.sort(key=lambda x: x.get("created_date",""), reverse=True)
        return jsonify({"transactions": txs})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False)

