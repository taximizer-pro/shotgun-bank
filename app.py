import os, json, hashlib, secrets, string, random, urllib.parse as _uparse, urllib.request
from flask import Flask, request, jsonify, session, redirect, url_for, render_template
import stripe

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# ── STRIPE ────────────────────────────────────────────────────────────────────
STRIPE_SK   = os.environ.get("STRIPE_SECRET_KEY","")
STRIPE_PK   = os.environ.get("STRIPE_PUBLISHABLE_KEY","")
STRIPE_WH   = os.environ.get("STRIPE_WEBHOOK_SECRET","")
stripe.api_key = STRIPE_SK

# ── BASE44 ────────────────────────────────────────────────────────────────────
B44_KEY  = os.environ.get("BASE44_API_KEY","")
B44_APP  = "6a13ae4b43ea85cec629af77"
B44_BASE = f"https://app.base44.com/api/apps/{B44_APP}/entities"
SG_URL   = f"{B44_BASE}/ShotgunAccount"
TX_URL   = f"{B44_BASE}/ShotgunTransaction"
CT_URL   = f"{B44_BASE}/ShotgunContact"

# ── FEE STRUCTURE ─────────────────────────────────────────────────────────────
FEE_CREDIT_CARD   = 0.04      # 4%
FEE_INSTANT_WD    = 0.0575    # 5.75%
FEE_STANDARD_WD   = 0.015     # 1.5%
FEE_REJECTED      = 5.00      # $5 flat
FEE_P2P_SENDER    = 1.50      # $1.50 sender
FEE_P2P_RECIPIENT = 1.50      # $1.50 recipient

def b44_headers():
    return {"Authorization": f"Bearer {B44_KEY}", "Content-Type": "application/json"}

def b44_get(url):
    req = urllib.request.Request(url, headers=b44_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def b44_post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST", headers=b44_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def b44_put(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="PUT", headers=b44_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def gen_routing():
    return "021" + str(random.randint(100000000, 999999999))

def gen_account():
    return str(random.randint(1000000000, 9999999999))

def gen_card():
    return "4" + "".join([str(random.randint(0,9)) for _ in range(15)])

def gen_cvv():
    return str(random.randint(100,999))

def gen_exp():
    return "12/28"

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", stripe_pk=STRIPE_PK)

@app.route("/admin")
def admin():
    if not session.get("admin"):
        return redirect("/admin/login")
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

# ── STRIPE PUBLISHABLE KEY ────────────────────────────────────────────────────
@app.route("/api/config")
def api_config():
    return jsonify({"publishable_key": STRIPE_PK})

# ── HASHTAG CHECK ──────────────────────────────────────────────────────────────
@app.route("/api/check-hashtag")
def check_hashtag():
    tag = request.args.get("tag","").strip().lower()
    if not tag or len(tag) < 3:
        return jsonify({"available": False, "reason": "Too short"})
    try:
        records = b44_get(f"{SG_URL}?hashtag={_uparse.quote(tag)}&limit=1")
        available = not bool(records if isinstance(records, list) else records.get("results",[]))
        return jsonify({"available": available})
    except:
        return jsonify({"available": False})

# ── SIGNUP — Step 1: Create account + Stripe Connect ──────────────────────────
@app.route("/api/signup", methods=["POST"])
def signup():
    d = request.json or {}
    first = d.get("first_name","").strip()
    last  = d.get("last_name","").strip()
    email = d.get("email","").strip().lower()
    phone = d.get("phone","").strip()
    tag   = d.get("hashtag","").strip().lower().replace("#","")
    pin   = d.get("pin","").strip()
    dob   = d.get("dob","").strip()  # YYYY-MM-DD

    if not all([first, last, email, tag, pin, dob]):
        return jsonify({"error": "All fields required"}), 400
    if len(pin) != 4 or not pin.isdigit():
        return jsonify({"error": "PIN must be 4 digits"}), 400

    try:
        # Check hashtag
        existing = b44_get(f"{SG_URL}?hashtag={_uparse.quote(tag)}&limit=1")
        if existing if isinstance(existing, list) else existing.get("results",[]):
            return jsonify({"error": "Hashtag taken"}), 409

        # Create Stripe Connect Express account
        dob_parts = dob.split("-")
        stripe_acct = stripe.Account.create(
            type="express",
            email=email,
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
            business_type="individual",
            individual={
                "first_name": first,
                "last_name":  last,
                "email":      email,
                "phone":      phone or None,
                "dob": {
                    "year":  int(dob_parts[0]),
                    "month": int(dob_parts[1]),
                    "day":   int(dob_parts[2]),
                } if len(dob_parts) == 3 else None,
            },
            metadata={"shotgun_hashtag": tag, "platform": "shotgun_bank"},
            settings={"payouts": {"schedule": {"interval": "manual"}}},
        )

        # Create Stripe onboarding link
        base_url = request.host_url.rstrip("/")
        onboard_link = stripe.AccountLink.create(
            account=stripe_acct.id,
            refresh_url=f"{base_url}/onboard/refresh?tag={tag}",
            return_url=f"{base_url}/onboard/complete?tag={tag}",
            type="account_onboarding",
        )

        # Save to Base44
        acct_data = {
            "first_name":          first,
            "last_name":           last,
            "email":               email,
            "phone":               phone,
            "hashtag":             tag,
            "pin_hash":            hash_pin(pin),
            "status":              "onboarding",
            "balance":             0.0,
            "wise_account_id":     stripe_acct.id,
            "routing_number":      gen_routing(),
            "account_number":      gen_account(),
            "virtual_card_number": gen_card(),
            "virtual_card_cvv":    gen_cvv(),
            "virtual_card_expiry": gen_exp(),
            "beat_v_enabled":      False,
            "beat_v_used":         False,
            "lifetime_deposited":  0.0,
            "funded_friends_count":0,
        }
        saved = b44_post(SG_URL, acct_data)
        acct_id = saved.get("id","")

        return jsonify({
            "success":       True,
            "account_id":    acct_id,
            "stripe_account": stripe_acct.id,
            "onboarding_url": onboard_link.url,
        })
    except stripe.error.StripeError as e:
        return jsonify({"error": str(e.user_message or e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── ONBOARD COMPLETE ──────────────────────────────────────────────────────────
@app.route("/onboard/complete")
def onboard_complete():
    tag = request.args.get("tag","")
    return render_template("onboard_complete.html", tag=tag)

@app.route("/onboard/refresh")
def onboard_refresh():
    tag = request.args.get("tag","")
    # Re-generate onboarding link
    try:
        records = b44_get(f"{SG_URL}?hashtag={_uparse.quote(tag)}&limit=1")
        acct = (records[0] if isinstance(records,list) else records.get("results",[])[0]) if records else None
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

# ── LOGIN ──────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    d = request.json or {}
    identifier = d.get("identifier","").strip().lower().replace("#","")
    pin        = d.get("pin","").strip()
    if not identifier or not pin:
        return jsonify({"error": "Missing credentials"}), 400
    try:
        records = b44_get(f"{SG_URL}?email={_uparse.quote(identifier)}&limit=1")
        if not records or (isinstance(records,list) and not records):
            records = b44_get(f"{SG_URL}?hashtag={_uparse.quote(identifier)}&limit=1")
        acct = (records[0] if isinstance(records,list) else None) if records else None
        if not acct:
            return jsonify({"error": "Account not found"}), 404
        if acct.get("status") == "onboarding":
            return jsonify({"status": "onboarding", "stripe_account": acct.get("wise_account_id","")})
        if acct.get("status") == "pending":
            return jsonify({"status": "pending"})
        if acct.get("status") == "denied":
            return jsonify({"error": "Account not approved"}), 403
        if acct.get("pin_hash") != hash_pin(pin):
            return jsonify({"error": "Incorrect PIN"}), 401
        # Must have payment method linked
        has_payment = bool(acct.get("linked_card_last4") or acct.get("linked_routing"))
        b44_put(f"{SG_URL}/{acct['id']}", {"is_online": True})
        return jsonify({"success": True, "account": acct, "has_payment": has_payment})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── ADD PAYMENT METHOD (bank or card via Stripe) ───────────────────────────────
@app.route("/api/setup-payment", methods=["POST"])
def setup_payment():
    """Create a Stripe SetupIntent for saving a card or bank account."""
    d = request.json or {}
    sg_id = d.get("sg_account_id","")
    try:
        records = b44_get(f"{SG_URL}/{sg_id}")
        acct = records if isinstance(records,dict) else (records[0] if records else None)
        if not acct: return jsonify({"error":"Account not found"}), 404
        stripe_acct_id = acct.get("wise_account_id","")
        # Create a Customer on the connected account for storing payment methods
        customer = stripe.Customer.create(
            email=acct.get("email",""),
            name=f"{acct.get('first_name','')} {acct.get('last_name','')}".strip(),
            stripe_account=stripe_acct_id,
        )
        # Save customer ID
        b44_put(f"{SG_URL}/{sg_id}", {"linked_card_last4": "setup_pending"})
        setup_intent = stripe.SetupIntent.create(
            customer=customer.id,
            payment_method_types=["card", "us_bank_account"],
            stripe_account=stripe_acct_id,
            metadata={"sg_account_id": sg_id},
        )
        return jsonify({"success": True, "client_secret": setup_intent.client_secret, "stripe_account": stripe_acct_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── DEPOSIT (card — 4% fee) ────────────────────────────────────────────────────
@app.route("/api/deposit", methods=["POST"])
def deposit():
    d = request.json or {}
    sg_id  = d.get("sg_account_id","")
    amount = float(d.get("amount",0))
    pm_id  = d.get("payment_method_id","")  # Stripe PM id
    if not sg_id or amount < 1:
        return jsonify({"error": "Missing fields"}), 400
    try:
        records = b44_get(f"{SG_URL}/{sg_id}")
        acct = records if isinstance(records,dict) else (records[0] if records else None)
        if not acct: return jsonify({"error":"Account not found"}), 404
        stripe_acct_id = acct.get("wise_account_id","")
        amount_cents = int(amount * 100)
        fee_cents    = int(amount * FEE_CREDIT_CARD * 100)
        # Charge on connected account, platform takes fee
        pi = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            payment_method=pm_id,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            application_fee_amount=fee_cents,
            stripe_account=stripe_acct_id,
            metadata={"sg_account_id": sg_id, "type": "deposit"},
        )
        if pi.status == "succeeded":
            net = amount - (fee_cents / 100)
            new_bal = float(acct.get("balance",0)) + net
            new_dep = float(acct.get("lifetime_deposited",0)) + amount
            b44_put(f"{SG_URL}/{sg_id}", {"balance": round(new_bal,2), "lifetime_deposited": round(new_dep,2)})
            b44_post(TX_URL, {
                "from_account_id": "external",
                "to_account_id":   sg_id,
                "to_hashtag":      acct.get("hashtag",""),
                "to_name":         f"{acct.get('first_name','')} {acct.get('last_name','')}",
                "amount":          amount,
                "fee":             fee_cents/100,
                "net_amount":      net,
                "type":            "deposit",
                "status":          "completed",
                "note":            "Card deposit",
            })
            return jsonify({"success": True, "new_balance": round(new_bal,2), "fee": fee_cents/100})
        else:
            return jsonify({"error": "Payment not completed", "status": pi.status}), 400
    except stripe.error.CardError as e:
        # Charge $5 rejection fee
        _charge_rejection_fee(sg_id)
        return jsonify({"error": e.user_message or "Card declined", "rejection_fee": True}), 402
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _charge_rejection_fee(sg_id):
    try:
        records = b44_get(f"{SG_URL}/{sg_id}")
        acct = records if isinstance(records,dict) else (records[0] if records else None)
        if not acct: return
        new_bal = float(acct.get("balance",0)) - FEE_REJECTED
        b44_put(f"{SG_URL}/{sg_id}", {"balance": round(new_bal,2)})
        b44_post(TX_URL, {
            "from_account_id": sg_id,
            "to_account_id":   "platform",
            "from_hashtag":    acct.get("hashtag",""),
            "amount":          FEE_REJECTED,
            "fee":             FEE_REJECTED,
            "net_amount":      FEE_REJECTED,
            "type":            "rejection_fee",
            "status":          "completed",
            "note":            "Rejected payment fee",
        })
    except: pass

# ── WITHDRAW — Instant (5.75%) or Standard (1.5%) ─────────────────────────────
@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d = request.json or {}
    sg_id  = d.get("sg_account_id","")
    amount = float(d.get("amount",0))
    speed  = d.get("speed","standard")  # "instant" or "standard"
    if not sg_id or amount < 1:
        return jsonify({"error": "Missing fields"}), 400
    fee_pct  = FEE_INSTANT_WD if speed == "instant" else FEE_STANDARD_WD
    fee_amt  = round(amount * fee_pct, 2)
    net_amt  = round(amount - fee_amt, 2)
    try:
        records = b44_get(f"{SG_URL}/{sg_id}")
        acct = records if isinstance(records,dict) else (records[0] if records else None)
        if not acct: return jsonify({"error":"Account not found"}), 404
        bal = float(acct.get("balance",0))
        if amount > bal: return jsonify({"error": f"Insufficient balance (${bal:.2f})"}), 400
        stripe_acct_id = acct.get("wise_account_id","")
        if not stripe_acct_id: return jsonify({"error":"No Stripe account linked"}), 400
        # Transfer net to connected account then payout
        transfer = stripe.Transfer.create(
            amount=int(net_amt * 100),
            currency="usd",
            destination=stripe_acct_id,
            metadata={"sg_account_id": sg_id, "type": f"{speed}_withdrawal"},
        )
        payout = stripe.Payout.create(
            amount=int(net_amt * 100),
            currency="usd",
            method="instant" if speed == "instant" else "standard",
            stripe_account=stripe_acct_id,
        )
        new_bal = bal - amount
        b44_put(f"{SG_URL}/{sg_id}", {"balance": round(new_bal,2)})
        b44_post(TX_URL, {
            "from_account_id": sg_id,
            "to_account_id":   "external",
            "from_hashtag":    acct.get("hashtag",""),
            "amount":          amount,
            "fee":             fee_amt,
            "net_amount":      net_amt,
            "type":            f"{speed}_withdrawal",
            "status":          "completed",
            "note":            f"{speed.title()} withdrawal to bank",
        })
        return jsonify({"success": True, "new_balance": round(new_bal,2), "fee": fee_amt, "net": net_amt, "payout_id": payout.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── P2P SEND ──────────────────────────────────────────────────────────────────
@app.route("/api/send", methods=["POST"])
def send_money():
    d = request.json or {}
    from_id    = d.get("from_account_id","")
    to_hashtag = d.get("to_hashtag","").lower().replace("#","")
    amount     = float(d.get("amount",0))
    note       = d.get("note","")
    if not from_id or not to_hashtag or amount <= 0:
        return jsonify({"error": "Missing fields"}), 400
    total_debit = amount + FEE_P2P_SENDER
    try:
        sender_rec  = b44_get(f"{SG_URL}/{from_id}")
        sender      = sender_rec if isinstance(sender_rec,dict) else (sender_rec[0] if sender_rec else None)
        recip_list  = b44_get(f"{SG_URL}?hashtag={_uparse.quote(to_hashtag)}&limit=1")
        recipient   = (recip_list[0] if isinstance(recip_list,list) else None) if recip_list else None
        if not sender:    return jsonify({"error":"Sender not found"}), 404
        if not recipient: return jsonify({"error":"Recipient not found — check the #hashtag"}), 404
        if sender.get("id") == recipient.get("id"): return jsonify({"error":"Can't send to yourself"}), 400
        sender_bal = float(sender.get("balance",0))
        if total_debit > sender_bal:
            if not sender.get("beat_v_enabled"):
                return jsonify({"error": f"Insufficient balance. Need ${total_debit:.2f}, have ${sender_bal:.2f}"}), 400
            if sender_bal - total_debit < -100:
                return jsonify({"error":"Beat the V limit reached (-$100 max)"}), 400
        # Credit recipient minus their fee
        net_to_recip = amount - FEE_P2P_RECIPIENT
        new_sender_bal = round(sender_bal - total_debit, 2)
        new_recip_bal  = round(float(recipient.get("balance",0)) + net_to_recip, 2)
        b44_put(f"{SG_URL}/{sender['id']}", {"balance": new_sender_bal})
        b44_put(f"{SG_URL}/{recipient['id']}", {"balance": new_recip_bal})
        b44_post(TX_URL, {
            "from_account_id": sender["id"],
            "to_account_id":   recipient["id"],
            "from_hashtag":    sender.get("hashtag",""),
            "to_hashtag":      recipient.get("hashtag",""),
            "from_name":       f"{sender.get('first_name','')} {sender.get('last_name','')}",
            "to_name":         f"{recipient.get('first_name','')} {recipient.get('last_name','')}",
            "amount":          amount,
            "fee":             FEE_P2P_SENDER + FEE_P2P_RECIPIENT,
            "net_amount":      net_to_recip,
            "type":            "p2p",
            "status":          "completed",
            "note":            note,
        })
        return jsonify({"success": True, "new_balance": new_sender_bal, "fee_charged": FEE_P2P_SENDER})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── TRANSACTIONS ───────────────────────────────────────────────────────────────
@app.route("/api/transactions/<sg_id>")
def transactions(sg_id):
    try:
        all_tx = b44_get(f"{TX_URL}?limit=50")
        txs = [t for t in (all_tx if isinstance(all_tx,list) else all_tx.get("results",[])) if t.get("from_account_id")==sg_id or t.get("to_account_id")==sg_id]
        txs.sort(key=lambda x: x.get("created_date",""), reverse=True)
        return jsonify({"transactions": txs[:30]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── STRIPE WEBHOOK ─────────────────────────────────────────────────────────────
@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature","")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH) if STRIPE_WH else json.loads(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    evt_type = event.get("type","")
    data_obj = event.get("data",{}).get("object",{})
    # Account fully verified by Stripe
    if evt_type == "account.updated":
        acct_id = data_obj.get("id","")
        charges_enabled = data_obj.get("charges_enabled", False)
        payouts_enabled = data_obj.get("payouts_enabled", False)
        if charges_enabled and payouts_enabled:
            try:
                records = b44_get(f"{SG_URL}?wise_account_id={_uparse.quote(acct_id)}&limit=1")
                acct = (records[0] if isinstance(records,list) else None) if records else None
                if acct and acct.get("status") == "onboarding":
                    b44_put(f"{SG_URL}/{acct['id']}", {"status": "pending"})  # Shotgun admin approves final
            except: pass
    # Payment succeeded — credit balance
    if evt_type == "payment_intent.succeeded":
        sg_id = data_obj.get("metadata",{}).get("sg_account_id","")
        if sg_id and data_obj.get("metadata",{}).get("type") == "deposit":
            pass  # Handled synchronously in /api/deposit
    return jsonify({"received": True})

# ── ADMIN APIs ─────────────────────────────────────────────────────────────────
@app.route("/api/admin/pending")
def admin_pending():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        records = b44_get(f"{SG_URL}?status=pending&limit=100")
        return jsonify({"accounts": records if isinstance(records,list) else records.get("results",[])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/all")
def admin_all():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        records = b44_get(f"{SG_URL}?limit=500")
        return jsonify({"accounts": records if isinstance(records,list) else records.get("results",[])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/approve/<sg_id>", methods=["POST"])
def admin_approve(sg_id):
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        b44_put(f"{SG_URL}/{sg_id}", {"status": "approved"})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/deny/<sg_id>", methods=["POST"])
def admin_deny(sg_id):
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    d = request.json or {}
    try:
        b44_put(f"{SG_URL}/{sg_id}", {"status": "denied", "denied_reason": d.get("reason","")})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/transactions")
def admin_transactions():
    if not session.get("admin"): return jsonify({"error":"Unauthorized"}), 401
    try:
        records = b44_get(f"{TX_URL}?limit=100")
        txs = records if isinstance(records,list) else records.get("results",[])
        txs.sort(key=lambda x: x.get("created_date",""), reverse=True)
        return jsonify({"transactions": txs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False)
