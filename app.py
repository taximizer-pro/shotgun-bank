import os, json, hashlib, secrets, string, random, urllib.parse as _uparse, urllib.request, time, smtplib
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, session, redirect, render_template
import stripe
from flask import Flask, request, jsonify, session, redirect, render_template
import stripe

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# ── STRIPE ────────────────────────────────────────────────────────────────────
STRIPE_SK  = os.environ.get("STRIPE_SECRET_KEY","")
STRIPE_PK  = os.environ.get("STRIPE_PUBLISHABLE_KEY","")
STRIPE_WH  = os.environ.get("STRIPE_WEBHOOK_SECRET","")
stripe.api_key = STRIPE_SK

# ── BASE44 ────────────────────────────────────────────────────────────────────
B44_KEY  = os.environ.get("BASE44_API_KEY","")
B44_APP  = "6a14ef767988d1ef0baff5aa"
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
    return {"Authorization": f"Bearer {B44_KEY}", "Content-Type": "application/json"}

def b44_get(url):
    req = urllib.request.Request(url, headers=b44h())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def b44_post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST", headers=b44h())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def b44_put(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="PUT", headers=b44h())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def hash_pin(pin): return hashlib.sha256(pin.encode()).hexdigest()
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

def get_crypto_price(symbol):
    """Fetch live crypto price via CoinGecko (free, no key)."""
    ids = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","USDC":"usd-coin","DOGE":"dogecoin"}
    cg_id = ids.get(symbol.upper())
    if not cg_id: return None
    try:
        req = urllib.request.Request(
            f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
            headers={"User-Agent":"shotgun-bank/1.0"})
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read())
            return data.get(cg_id,{}).get("usd")
    except:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# PAGE ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", stripe_pk=STRIPE_PK)

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
        return jsonify({"available": False, "reason": "Too short"})
    try:
        acct = get_acct_by_tag(tag)
        return jsonify({"available": acct is None})
    except Exception as e:
        print(f"[HASHTAG CHECK ERROR] {e}")
        # Fail open — don't block signup on API errors
        return jsonify({"available": True})

# ─────────────────────────────────────────────────────────────────────────────
# SIGNUP — creates Stripe Connect Express + Base44 record
# ─────────────────────────────────────────────────────────────────────────────

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

        pw_hash = hashlib.sha256(pw.encode()).hexdigest()
        saved = b44_post(SG_URL, {
            "first_name": first, "last_name": last,
            "full_name": f"{first} {last}",
            "email": email, "phone": phone, "hashtag": tag,
            "dob": dob, "pin_hash": hash_pin(pin),
            "password_hash": pw_hash,
            "status": "pending", "balance": 0.0,
            "routing_number": gen_routing(),
            "account_number": gen_account(),
            "virtual_card_number": gen_card(),
            "virtual_card_cvv": gen_cvv(),
            "virtual_card_expiry": gen_exp(),
            "beat_v_enabled": False, "beat_v_used": False,
            "lifetime_deposited": 0.0, "funded_friends_count": 0,
        })

        return jsonify({
            "success": True,
            "account_id": saved.get("id",""),
            "status": "pending",
            "message": "Account created! You will receive an email once approved."
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
        if status == "onboarding":
            return jsonify({"status":"onboarding","stripe_account":acct.get("wise_account_id",""),"account_id":acct.get("id","")})
        if status == "pending":
            return jsonify({"status":"pending","message":"Your account is under review."})
        if status == "denied":
            return jsonify({"error": "Account not approved. Contact support."}), 403
        if acct.get("pin_hash") != hash_pin(pin):
            return jsonify({"error": "Incorrect PIN"}), 401
        stored_pw = acct.get("password_hash","")
        import hashlib as _hl
        if stored_pw and stored_pw != _hl.sha256(password.encode()).hexdigest():
            return jsonify({"error": "Incorrect password"}), 401
        # Credentials OK — generate + send 2FA OTP
        acct_id = acct["id"]
        code    = str(__import__('random').randint(100000,999999))
        email   = acct.get("email","")
        _otp_store[acct_id] = {"code": code, "expires": time.time() + OTP_TTL, "email": email}
        send_otp_email(email, code, acct.get("first_name",""))
        masked = email[:2] + "***@" + email.split("@")[-1] if "@" in email else "your email"
        return jsonify({"requires_2fa": True, "account_id": acct_id, "email_masked": masked})
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


@app.route("/api/link-bank/session", methods=["POST"])
def link_bank_session():
    """Step 1: Create a Financial Connections session for instant bank verification."""
    d = request.json or {}
    sg_id = d.get("sg_account_id","")
    if not sg_id: return jsonify({"error":"Missing account"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404

        # Get or create Stripe customer for this user
        stripe_acct_id = acct.get("wise_account_id","")
        customer_email = acct.get("email","")

        # Create customer on the platform account (not connected account)
        # to enable Financial Connections
        existing_customers = stripe.Customer.list(email=customer_email, limit=1)
        if existing_customers.data:
            customer = existing_customers.data[0]
        else:
            customer = stripe.Customer.create(
                email=customer_email,
                name=f"{acct.get('first_name','')} {acct.get('last_name','')}",
                metadata={"sg_account_id": sg_id, "hashtag": acct.get("hashtag","")},
            )

        # Create Financial Connections Session
        # This verifies ownership + enables instant ACH
        fc_session = stripe.financial_connections.Session.create(
            account_holder={"type": "customer", "customer": customer.id},
            permissions=["payment_method", "balances", "ownership"],
            filters={"countries": ["US"]},
        )

        return jsonify({
            "success": True,
            "client_secret": fc_session.client_secret,
            "customer_id": customer.id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/link-bank/confirm", methods=["POST"])
def link_bank_confirm():
    """Step 2: After FC session completes, attach the bank account as a payment method."""
    d = request.json or {}
    sg_id       = d.get("sg_account_id","")
    fc_account  = d.get("financial_connections_account","")  # fc_acct_xxx id from JS
    customer_id = d.get("customer_id","")
    if not all([sg_id, fc_account, customer_id]):
        return jsonify({"error":"Missing fields"}), 400
    try:
        acct = get_acct(sg_id)
        if not acct: return jsonify({"error":"Account not found"}), 404

        # Create a PaymentMethod from the FC account
        pm = stripe.PaymentMethod.create(
            type="us_bank_account",
            us_bank_account={"financial_connections_account": fc_account},
        )

        # Attach to customer
        pm = stripe.PaymentMethod.attach(pm.id, customer=customer_id)

        # Retrieve FC account details for display
        fc_acct_obj = stripe.financial_connections.Account.retrieve(fc_account)
        bank_name   = fc_acct_obj.institution_name or "Bank"
        last4       = fc_acct_obj.last4 or "****"
        acct_type   = fc_acct_obj.subcategory or "checking"

        # Save to Base44
        b44_put(f"{SG_URL}/{sg_id}", {
            "linked_routing": fc_acct_obj.routing_number or "",
            "linked_account": fc_account,
            "linked_card_last4": last4,
        })

        # Save PM id and customer id in a note field for future charges
        b44_put(f"{SG_URL}/{sg_id}", {
            "payment_notes": json.dumps({
                "stripe_customer_id": customer_id,
                "stripe_pm_id": pm.id,
                "bank_name": bank_name,
                "last4": last4,
                "type": acct_type,
            })
        })

        return jsonify({
            "success": True,
            "bank_name": bank_name,
            "last4": last4,
            "account_type": acct_type,
            "payment_method_id": pm.id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# DEPOSIT — Card (4%) or ACH/Bank (free) or manual top-up
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/deposit", methods=["POST"])
def deposit():
    d      = request.json or {}
    sg_id  = d.get("sg_account_id","")
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
            # Charge card directly on connected account
            pi = stripe.PaymentIntent.create(
                amount=amt_cents,
                currency="usd",
                payment_method=pm_id,
                customer=customer_id or None,
                confirm=True,
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                application_fee_amount=fee_cents,
                stripe_account=stripe_acct_id,
                metadata={"sg_account_id": sg_id, "type":"deposit"},
            )
            if pi.status != "succeeded":
                return jsonify({"error":"Payment not completed","status":pi.status}), 400
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

# ─────────────────────────────────────────────────────────────────────────────
# WITHDRAW — Instant (5.75%) to debit card | Standard (1.5%) ACH
# ─────────────────────────────────────────────────────────────────────────────

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
        if not stripe_acct_id:
            return jsonify({"error":"No Stripe account linked. Complete onboarding first."}), 400
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
    from_id    = d.get("from_account_id","")
    to_hashtag = d.get("to_hashtag","").lower().replace("#","")
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
    """Live crypto prices for display."""
    prices = {}
    for sym, cg_id in [("BTC","bitcoin"),("ETH","ethereum"),("SOL","solana"),("USDC","usd-coin"),("DOGE","dogecoin")]:
        try:
            req = urllib.request.Request(
                f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd&include_24hr_change=true",
                headers={"User-Agent":"shotgun-bank/1.0"})
            with urllib.request.urlopen(req, timeout=8) as res:
                data = json.loads(res.read())
                prices[sym] = {
                    "usd": data.get(cg_id,{}).get("usd",0),
                    "change_24h": round(data.get(cg_id,{}).get("usd_24h_change",0), 2),
                }
        except:
            prices[sym] = {"usd": 0, "change_24h": 0}
    return jsonify({"prices": prices})

@app.route("/api/crypto/convert", methods=["POST"])
def crypto_convert():
    """Convert USD balance to crypto (simulated — records the conversion, no on-chain tx)."""
    d      = request.json or {}
    sg_id  = d.get("sg_account_id","")
    amount = float(d.get("amount_usd", 0))
    symbol = d.get("symbol","BTC").upper()
    if not sg_id or amount < 1: return jsonify({"error":"Minimum $1 to convert"}), 400
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
        return jsonify({"balance": acct.get("balance",0), "beat_v_enabled": acct.get("beat_v_enabled",False)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# STRIPE WEBHOOK — handles async events (ACH confirmed, account verified)
# ─────────────────────────────────────────────────────────────────────────────

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
        # Stripe Express account fully verified
        stripe_id = obj.get("id","")
        if obj.get("charges_enabled") and obj.get("payouts_enabled"):
            try:
                r = b44_get(f"{SG_URL}?wise_account_id={_uparse.quote(stripe_id)}&limit=1")
                lst = r if isinstance(r,list) else r.get("results",[])
                if lst and lst[0].get("status") == "onboarding":
                    b44_put(f"{SG_URL}/{lst[0]['id']}", {"status":"pending"})
            except: pass

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

@app.route("/api/admin/pending")
def admin_pending():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        r = b44_get(f"{SG_URL}?status=pending&limit=100")
        return jsonify({"accounts": r if isinstance(r,list) else r.get("results",[])})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/all")
def admin_all():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        r = b44_get(f"{SG_URL}?limit=500")
        return jsonify({"accounts": r if isinstance(r,list) else r.get("results",[])})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/approve/<sg_id>", methods=["POST"])
def admin_approve(sg_id):
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        b44_put(f"{SG_URL}/{sg_id}", {"status":"approved"})
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/admin/deny/<sg_id>", methods=["POST"])
def admin_deny(sg_id):
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    try:
        b44_put(f"{SG_URL}/{sg_id}", {"status":"denied","denied_reason":d.get("reason","")})
        return jsonify({"success":True})
    except Exception as e:
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
