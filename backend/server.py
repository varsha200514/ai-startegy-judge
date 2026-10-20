import os
import re

from dataclasses import asdict, dataclass
from datetime import date, datetime,timedelta
from threading import Lock
from typing import Any, Optional

import requests

from dotenv import load_dotenv
load_dotenv()

import requests
# pyrefly: ignore [missing-import]
from openai import OpenAI

from flask import Flask, jsonify, request
from flask_cors import CORS

# pyrefly: ignore [missing-import]
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    get_jwt_identity,
    jwt_required,
)

from sqlalchemy.exc import IntegrityError

from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

from database import (
    engine,
    Base,
    SessionLocal,
)

from models import (
    Stock as StockModel,
    User,
    Strategy,
    BacktestResult,
)

# ============================================================
# DATABASE
# ============================================================

Base.metadata.create_all(
    bind=engine
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": "*"
        }
    }
)


# ============================================================
# JWT CONFIGURATION
# ============================================================

JWT_SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY"
)

if not JWT_SECRET_KEY:

    raise RuntimeError(
        "JWT_SECRET_KEY is missing from .env"
    )

app.config[
    "JWT_SECRET_KEY"
] = JWT_SECRET_KEY

jwt = JWTManager(app)


# ============================================================
# UPSTOX CONFIGURATION
# ============================================================

UPSTOX_ACCESS_TOKEN = os.getenv(
    "UPSTOX_ACCESS_TOKEN"
)

UPSTOX_BASE_URL = (
    "https://api.upstox.com"
)


UPSTOX_INSTRUMENTS = {

    "RELIANCE":
        "NSE_EQ|INE002A01018",

    "TCS":
        "NSE_EQ|INE467B01029",

    "INFY":
        "NSE_EQ|INE009A01021",

    "SBIN":
        "NSE_EQ|INE062A01020",

    "HDFCBANK":
        "NSE_EQ|INE040A01034",
}


# ============================================================
# APPLICATION DATA
# ============================================================

RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "INR": 83.1,
}

# These are the Indian NSE instruments currently supported by the
# Upstox integration. Market prices are fetched live; they are not
# hard-coded demo prices.
STOCK_NAMES = {
    "RELIANCE": "Reliance Industries Ltd.",
    "TCS": "Tata Consultancy Services Ltd.",
    "INFY": "Infosys Ltd.",
    "SBIN": "State Bank of India",
    "HDFCBANK": "HDFC Bank Ltd.",
}


state = {

    "balance": 24850.42,

    "progress": 68,

    "positions": {}
}


state_lock = Lock()


# ============================================================
# GENERAL HELPERS
# ============================================================

def convert(
    amount: float,
    from_currency: str,
    to_currency: str
):

    if from_currency not in RATES:
        raise ValueError(
            "Unsupported source currency"
        )

    if to_currency not in RATES:
        raise ValueError(
            "Unsupported target currency"
        )

    usd_amount = (
        amount
        / RATES[from_currency]
    )

    return (
        usd_amount
        * RATES[to_currency]
    )


def stock_payload(stock):
    """
    Return a JSON-safe stock payload.

    Kept as a helper so the frontend contract stays simple while the
    underlying values now come from Upstox instead of demo data.
    """
    if isinstance(stock, dict):
        return stock
    return asdict(stock)


def user_response(user):

    return {

        "id": user.id,

        "name": user.name,

        "email": user.email,

        "is_active": user.is_active,

        "experience_level":
            user.experience_level,

        "risk_preference":
            user.risk_preference,

        "preferred_market":
            user.preferred_market,

        "profile_completed":
            user.profile_completed,

        "created_at":
            user.created_at.isoformat()
            if user.created_at
            else None,

    }


def strategy_response(strategy):

    return {

        "id": strategy.id,

        "user_id": strategy.user_id,

        "asset_symbol":
            strategy.asset_symbol,

        "entry_rule":
            strategy.entry_rule,

        "exit_rule":
            strategy.exit_rule,

        "time_horizon":
            strategy.time_horizon,

        "reasoning":
            strategy.reasoning,

        "failure_condition":
            strategy.failure_condition,

        "score":
            strategy.score,

        "risk_score":
            strategy.risk_score,

        "logic_score":
            strategy.logic_score,

        "status":
            strategy.status,

        "created_at":
            strategy.created_at.isoformat()
            if strategy.created_at
            else None,

    }


# ============================================================
# UPSTOX HELPERS
# ============================================================

def upstox_headers():

    if not UPSTOX_ACCESS_TOKEN:

        raise RuntimeError(
            "UPSTOX_ACCESS_TOKEN is missing from .env"
        )

    return {

        "Authorization":
            f"Bearer {UPSTOX_ACCESS_TOKEN}",

        "Accept":
            "application/json",

    }


def get_instrument_key(
    symbol: str
) -> Optional[str]:

    return UPSTOX_INSTRUMENTS.get(
        symbol.upper()
    )


def parse_upstox_candles(
    payload
):

    candles = (
        payload
        .get("data", {})
        .get("candles", [])
    )

    result = []

    for candle in candles:

        if len(candle) < 5:
            continue

        result.append({

            "timestamp":
                candle[0],

            "open":
                float(candle[1]),

            "high":
                float(candle[2]),

            "low":
                float(candle[3]),

            "close":
                float(candle[4]),

            "volume":
                float(candle[5])
                if len(candle) > 5
                else 0,

        })

    # Oldest -> newest
    result.reverse()

    return result


def upstox_get(path, params=None, timeout=15):
    """
    Make a GET request to Upstox and return decoded JSON.

    Keeping Upstox HTTP handling in one place makes the market routes
    easier to maintain and gives the frontend consistent errors.
    """
    response = requests.get(
        f"{UPSTOX_BASE_URL}{path}",
        headers=upstox_headers(),
        params=params or {},
        timeout=timeout,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Upstox request failed ({response.status_code}): "
            f"{response.text[:500]}"
        )

    return response.json()


def extract_upstox_quote(payload, symbol):
    """
    Extract the live OHLC object from the V3 OHLC response.

    Upstox uses keys such as NSE_EQ:RELIANCE in the response, while
    the frontend works with the simpler symbol RELIANCE.
    """
    data = payload.get("data") or {}
    symbol = symbol.upper()

    candidates = [
        f"NSE_EQ:{symbol}",
        f"NSE_EQ|{symbol}",
        symbol,
    ]

    item = None

    for key in candidates:
        if key in data:
            item = data[key]
            break

    if item is None and data:
        # The response normally contains exactly the requested
        # instrument, so using the first item is a safe fallback.
        item = next(iter(data.values()))

    if not item:
        raise RuntimeError(
            f"No market quote returned for {symbol}"
        )

    live = item.get("live_ohlc") or {}
    previous = item.get("prev_ohlc") or {}

    return {
        "instrument_token": item.get("instrument_token"),
        "last_price": item.get("last_price"),
        "open": live.get("open"),
        "high": live.get("high"),
        "low": live.get("low"),
        "volume": live.get("volume", 0),
        "timestamp": live.get("ts"),
        "previous_close": previous.get("close"),
    }


def get_previous_close_from_history(symbol):
    """
    Get the most recent completed daily close when the live quote
    does not include prev_ohlc.close.
    """
    instrument_key = get_instrument_key(symbol)

    if not instrument_key:
        return None

    to_date = date.today()
    from_date = to_date - timedelta(days=7)

    payload = upstox_get(
        f"/v3/historical-candle/"
        f"{instrument_key}/days/1/"
        f"{to_date.isoformat()}/"
        f"{from_date.isoformat()}",
        timeout=15,
    )

    candles = parse_upstox_candles(payload)

    if not candles:
        return None

    # The parser returns oldest -> newest. Use the last completed
    # daily candle as the previous close.
    return float(candles[-1]["close"])
def get_market_status():
    """
    Get NSE market status.

    Uses Upstox status when available.
    If Upstox does not return a usable status,
    determine open/closed from NSE trading hours.
    """

    try:
        # ====================================================
        # 1. TRY UPSTOX FIRST
        # ====================================================

        payload = upstox_get(
            "/v2/market/status/NSE",
            timeout=10,
        )

        data = payload.get("data") or {}

        status = None
        market_type = None

        if isinstance(data, dict):

            nse_data = data.get("NSE")

            if isinstance(nse_data, dict):
                status = nse_data.get("status")
                market_type = nse_data.get(
                    "market_type"
                )

            if status is None:

                for value in data.values():

                    if isinstance(value, dict):

                        status = (
                            value.get("status")
                            or status
                        )

                        market_type = (
                            value.get("market_type")
                            or market_type
                        )

        normalized = str(
            status or "UNKNOWN"
        ).upper()

        # ====================================================
        # 2. IF UPSTOX GIVES A USABLE STATUS
        # ====================================================

        if normalized in {
            "NORMAL_OPEN",
            "OPEN",
            "OPEN_NORMAL",
            "NORMAL_OPENING",
        }:

            return {
                "exchange": "NSE",
                "status": "OPEN",
                "is_open": True,
                "market_type": market_type,
            }

        if normalized in {
            "NORMAL_CLOSE",
            "CLOSED",
            "CLOSE",
            "CLOSED_NORMAL",
        }:

            return {
                "exchange": "NSE",
                "status": "CLOSED",
                "is_open": False,
                "market_type": market_type,
            }

        # ====================================================
        # 3. FALLBACK — NSE MARKET HOURS
        # ====================================================

        from datetime import (
            datetime,
            time,
            timezone,
            timedelta,
        )

        # India Standard Time = UTC + 5:30
        india_timezone = timezone(
            timedelta(
                hours=5,
                minutes=30
            )
        )

        india_time = datetime.now(
            india_timezone
        )

        current_time = india_time.time()
        weekday = india_time.weekday()

        # Saturday = 5
        # Sunday = 6
        if weekday >= 5:

            return {
                "exchange": "NSE",
                "status": "CLOSED",
                "is_open": False,
                "market_type": "WEEKEND",
            }

        # NSE regular equity market:
        # 9:15 AM to 3:30 PM IST

        market_open = time(
            9,
            15
        )

        market_close = time(
            15,
            30
        )

        if (
            current_time >= market_open
            and current_time < market_close
        ):

            return {
                "exchange": "NSE",
                "status": "OPEN",
                "is_open": True,
                "market_type": "REGULAR",
            }

        return {
            "exchange": "NSE",
            "status": "CLOSED",
            "is_open": False,
            "market_type": "REGULAR",
        }

    except Exception as exc:

        # ====================================================
        # 4. FINAL FALLBACK
        # ====================================================

        return {
            "exchange": "NSE",
            "status": "UNKNOWN",
            "is_open": None,
            "market_type": None,
            "error": str(exc),
        }

        


def build_market_stock(symbol):
    """
    Build the market stock card used by the frontend
    from real Upstox data.
    """

    symbol = symbol.upper()

    # ====================================================
    # GET LIVE OHLC DATA
    # ====================================================

    quote_payload = upstox_get(
        "/v3/market-quote/ohlc",
        params={
            "instrument_key":
                get_instrument_key(symbol),
            "interval": "1d",
        },
    )

    quote = extract_upstox_quote(
        quote_payload,
        symbol,
    )

    previous_close = quote[
        "previous_close"
    ]

    # Some Upstox responses can return prev_ohlc
    # as null. In that case use historical data.
    if previous_close is None:

        previous_close = (
            get_previous_close_from_history(
                symbol
            )
        )

    price = quote["last_price"]

    if price is None:

        raise RuntimeError(
            f"Live price was not returned for {symbol}"
        )

    # ====================================================
    # CALCULATE CHANGE
    # ====================================================

    change = None
    change_percent = None

    if previous_close not in (
        None,
        0
    ):

        change = (
            float(price)
            - float(previous_close)
        )

        change_percent = (
            change
            / float(previous_close)
        ) * 100

    # ====================================================
    # CREATE SPARKLINE
    # ====================================================

    points = ""

    try:

        to_date = date.today()

        from_date = (
            to_date
            - timedelta(days=10)
        )

        history_payload = upstox_get(
            f"/v3/historical-candle/"
            f"{get_instrument_key(symbol)}/days/1/"
            f"{to_date.isoformat()}/"
            f"{from_date.isoformat()}",
            timeout=15,
        )

        history = parse_upstox_candles(
            history_payload
        )

        closes = [
            float(candle["close"])
            for candle in history
            if candle.get("close") is not None
        ]

        if closes:

            low = min(closes)
            high = max(closes)

            span = high - low

            if span == 0:

                y_values = [
                    10
                    for _ in closes
                ]

            else:

                y_values = [

                    round(
                        18
                        - (
                            (
                                value
                                - low
                            )
                            / span
                            * 15
                        ),
                        2,
                    )

                    for value in closes
                ]

            step = (
                98
                / max(
                    len(y_values) - 1,
                    1
                )
            )

            points = " ".join(

                f"{round(2 + index * step, 2)},{y}"

                for index, y
                in enumerate(y_values)
            )

    except Exception:

        # Sparkline is optional.
        # Stock card should still work.
        points = ""

    # ====================================================
    # RETURN STOCK DATA
    # ====================================================

    return {
        "symbol": symbol,

        "name": STOCK_NAMES.get(
            symbol,
            symbol
        ),

        "price": round(
            float(price),
            2
        ),

        "change": (
            round(change, 2)
            if change is not None
            else None
        ),

        "change_percent": (
            round(change_percent, 2)
            if change_percent is not None
            else None
        ),

        "open": quote["open"],

        "high": quote["high"],

        "low": quote["low"],

        "close": round(
            float(price),
            2
        ),

        "previous_close": (
            round(
                float(previous_close),
                2
            )
            if previous_close is not None
            else None
        ),

        "volume": quote["volume"],

        "timestamp": quote["timestamp"],

        "points": points,
    }

# ============================================================
# AUTH - REGISTER
# ============================================================

@app.route(
    "/api/auth/register",
    methods=["POST"]
)
def register():

    data = request.get_json(
        silent=True
    ) or {}

    name = str(
        data.get("name", "")
    ).strip()

    email = str(
        data.get("email", "")
    ).strip().lower()

    password = str(
        data.get("password", "")
    )

    if not name:

        return jsonify({
            "error": "Name is required"
        }), 400

    if not email:

        return jsonify({
            "error": "Email is required"
        }), 400

    if len(password) < 6:

        return jsonify({
            "error":
                "Password must contain at least 6 characters"
        }), 400

    db = SessionLocal()

    try:

        existing = db.query(User).filter(
            User.email == email
        ).first()

        if existing:

            return jsonify({
                "error":
                    "Email already registered"
            }), 409

        user = User(

            name=name,

            email=email,

            password_hash=
                generate_password_hash(
                    password
                ),

            is_active=True,

            profile_completed=False

        )

        db.add(user)

        db.commit()

        db.refresh(user)

        token = create_access_token(
            identity=str(user.id)
        )

        return jsonify({

            "message":
                "Registration successful",

            "token":
                token,

            "user":
                user_response(user)

        }), 201

    except IntegrityError:

        db.rollback()

        return jsonify({
            "error":
                "Email already registered"
        }), 409

    finally:

        db.close()


# ============================================================
# AUTH - LOGIN
# ============================================================

@app.route(
    "/api/auth/login",
    methods=["POST"]
)
def login():

    data = request.get_json(
        silent=True
    ) or {}

    email = str(
        data.get("email", "")
    ).strip().lower()

    password = str(
        data.get("password", "")
    )

    if not email or not password:

        return jsonify({
            "error":
                "Email and password are required"
        }), 400

    db = SessionLocal()

    try:

        user = db.query(User).filter(
            User.email == email
        ).first()

        if not user:

            return jsonify({
                "error":
                    "Invalid email or password"
            }), 401

        if not check_password_hash(
            user.password_hash,
            password
        ):

            return jsonify({
                "error":
                    "Invalid email or password"
            }), 401

        if not user.is_active:

            return jsonify({
                "error":
                    "User account is inactive"
            }), 403

        token = create_access_token(
            identity=str(user.id)
        )

        return jsonify({

            "message":
                "Login successful",

            "token":
                token,

            "user":
                user_response(user)

        }), 200

    finally:

        db.close()


# ============================================================
# AUTH - ME
# ============================================================

@app.route(
    "/api/auth/me",
    methods=["GET"]
)
@jwt_required()
def me():

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:

            return jsonify({
                "error":
                    "User not found"
            }), 404

        return jsonify({
            "user":
                user_response(user)
        }), 200

    finally:

        db.close()


# ============================================================
# AUTH - LOGOUT
# ============================================================

@app.route(
    "/api/auth/logout",
    methods=["POST"]
)
@jwt_required()
def logout():

    return jsonify({
        "message":
            "Logout successful"
    }), 200


# ============================================================
# PROFILE - GET
# ============================================================

@app.route(
    "/api/profile",
    methods=["GET"]
)
@jwt_required()
def get_profile():

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:

            return jsonify({
                "error":
                    "User not found"
            }), 404

        return jsonify({
            "profile":
                user_response(user)
        }), 200

    finally:

        db.close()


# ============================================================
# PROFILE - UPDATE
# ============================================================

@app.route(
    "/api/profile",
    methods=["PUT"]
)
@jwt_required()
def update_profile():

    user_id = int(
        get_jwt_identity()
    )

    data = request.get_json(
        silent=True
    ) or {}

    db = SessionLocal()

    try:

        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:

            return jsonify({
                "error":
                    "User not found"
            }), 404

        if "name" in data:

            user.name = str(
                data["name"]
            ).strip()

        if "experience_level" in data:

            user.experience_level = (
                data["experience_level"]
            )

        if "risk_preference" in data:

            user.risk_preference = (
                data["risk_preference"]
            )

        if "preferred_market" in data:

            user.preferred_market = (
                data["preferred_market"]
            )

        user.profile_completed = True

        db.commit()

        db.refresh(user)

        return jsonify({

            "message":
                "Profile updated successfully",

            "profile":
                user_response(user)

        }), 200

    finally:

        db.close()


# ============================================================
# STRATEGY - CREATE
# ============================================================

@app.route(
    "/api/strategies",
    methods=["POST"]
)
@jwt_required()
def create_strategy():

    user_id = int(
        get_jwt_identity()
    )

    data = request.get_json(
        silent=True
    ) or {}

    asset_symbol = str(
        data.get("asset_symbol", "")
    ).strip().upper()

    entry_rule = str(
        data.get("entry_rule", "")
    ).strip()

    exit_rule = str(
        data.get("exit_rule", "")
    ).strip()

    time_horizon = str(
        data.get("time_horizon", "")
    ).strip()

    reasoning = str(
        data.get("reasoning", "")
    ).strip()

    failure_condition = str(
        data.get("failure_condition", "")
    ).strip()

    if not asset_symbol:

        return jsonify({
            "error":
                "Asset symbol is required"
        }), 400

    if not entry_rule:

        return jsonify({
            "error":
                "Entry rule is required"
        }), 400

    if not exit_rule:

        return jsonify({
            "error":
                "Exit rule is required"
        }), 400

    if not time_horizon:

        return jsonify({
            "error":
                "Time horizon is required"
        }), 400

    if not reasoning:

        return jsonify({
            "error":
                "Reasoning is required"
        }), 400

    db = SessionLocal()

    try:

        strategy = Strategy(

            user_id=user_id,

            asset_symbol=asset_symbol,

            entry_rule=entry_rule,

            exit_rule=exit_rule,

            time_horizon=time_horizon,

            reasoning=reasoning,

            failure_condition=
                failure_condition or None,

            status="PENDING"

        )

        db.add(strategy)

        db.commit()

        db.refresh(strategy)

        return jsonify({

            "message":
                "Strategy created successfully",

            "strategy":
                strategy_response(strategy)

        }), 201

    finally:

        db.close()


# ============================================================
# STRATEGY - GET
# ============================================================

@app.route(
    "/api/strategies",
    methods=["GET"]
)
@jwt_required()
def get_strategies():

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        strategies = db.query(
            Strategy
        ).filter(
            Strategy.user_id == user_id
        ).order_by(
            Strategy.created_at.desc()
        ).all()

        return jsonify({

            "strategies": [
                strategy_response(strategy)
                for strategy in strategies
            ]

        }), 200

    finally:

        db.close()


# ============================================================
# MODULE 4 - STRATEGY EVALUATION
# ============================================================

def analyze_strategy_rules(
    strategy
):

    entry = (
        strategy.entry_rule
        .lower()
        .strip()
    )

    exit_rule = (
        strategy.exit_rule
        .lower()
        .strip()
    )

    reasoning = (
        strategy.reasoning
        .strip()
    )

    failure = (
        strategy.failure_condition
        or ""
    ).lower()

    logic_score = 40.0

    risk_score = 50.0

    strengths = []

    warnings = []

    suggestions = []

    # --------------------------------------------------------
    # ENTRY RULE
    # --------------------------------------------------------

    if len(entry) >= 15:

        logic_score += 10

        strengths.append(
            "Entry rule contains a specific condition."
        )

    else:

        warnings.append(
            "Entry rule is too short or vague."
        )

        suggestions.append(
            "Define a measurable entry condition."
        )

    # --------------------------------------------------------
    # EXIT RULE
    # --------------------------------------------------------

    if len(exit_rule) >= 10:

        logic_score += 10

        strengths.append(
            "Exit rule is defined."
        )

    else:

        warnings.append(
            "Exit rule is unclear."
        )

        suggestions.append(
            "Add a specific target or exit condition."
        )

    # --------------------------------------------------------
    # FAILURE CONDITION
    # --------------------------------------------------------

    if failure:

        risk_score += 15

        strengths.append(
            "Strategy contains a failure condition."
        )

    else:

        warnings.append(
            "No failure condition was provided."
        )

        suggestions.append(
            "Define when the strategy should be considered invalid."
        )

    # --------------------------------------------------------
    # RISK KEYWORDS
    # --------------------------------------------------------

    risk_keywords = [

        "stop loss",
        "stop-loss",
        "drawdown",
        "risk",
        "capital protection",
        "maximum loss"

    ]

    if any(
        keyword in exit_rule
        or keyword in failure
        for keyword in risk_keywords
    ):

        risk_score += 20

        strengths.append(
            "Risk-control concepts are present."
        )

    else:

        warnings.append(
            "No clear risk-control mechanism detected."
        )

        suggestions.append(
            "Consider adding stop-loss or capital-risk rules."
        )

    # --------------------------------------------------------
    # REASONING
    # --------------------------------------------------------

    if len(reasoning) >= 100:

        logic_score += 15

        strengths.append(
            "Reasoning provides substantial explanation."
        )

    elif len(reasoning) >= 50:

        logic_score += 8

    else:

        warnings.append(
            "Reasoning is relatively short."
        )

        suggestions.append(
            "Explain why the entry and exit conditions should work."
        )

    # --------------------------------------------------------
    # NUMBERS
    # --------------------------------------------------------

    if re.search(
        r"\d",
        entry + exit_rule
    ):

        logic_score += 10

        strengths.append(
            "Strategy includes measurable values."
        )

    else:

        warnings.append(
            "Strategy contains few measurable thresholds."
        )

        suggestions.append(
            "Use numerical thresholds where appropriate."
        )

    # --------------------------------------------------------
    # TIME HORIZON
    # --------------------------------------------------------

    if strategy.time_horizon:

        logic_score += 5

    # --------------------------------------------------------
    # LIMIT SCORES
    # --------------------------------------------------------

    logic_score = max(
        0,
        min(
            100,
            logic_score
        )
    )

    risk_score = max(
        0,
        min(
            100,
            risk_score
        )
    )

    score = (
        logic_score * 0.55
        + risk_score * 0.45
    )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if (
        score >= 75
        and risk_score >= 60
        and logic_score >= 65
    ):

        status = "PASS"

    elif score >= 50:

        status = "REVIEW"

    else:

        status = "REJECT"

    return {

        "score":
            round(score, 2),

        "logic_score":
            round(logic_score, 2),

        "risk_score":
            round(risk_score, 2),

        "status":
            status,

        "strengths":
            strengths,

        "warnings":
            warnings,

        "suggestions":
            suggestions
    }


@app.route(
    "/api/strategies/<int:strategy_id>/evaluate",
    methods=["POST"]
)
@jwt_required()
def evaluate_strategy(strategy_id):

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        strategy = db.query(
            Strategy
        ).filter(
            Strategy.id == strategy_id,
            Strategy.user_id == user_id
        ).first()

        if not strategy:

            return jsonify({
                "error":
                    "Strategy not found"
            }), 404

        result = analyze_strategy_rules(
            strategy
        )

        strategy.score = result[
            "score"
        ]

        strategy.logic_score = result[
            "logic_score"
        ]

        strategy.risk_score = result[
            "risk_score"
        ]

        strategy.status = result[
            "status"
        ]

        db.commit()

        db.refresh(strategy)

        return jsonify({

            "message":
                "Strategy evaluated successfully",

            "strategy":
                strategy_response(strategy),

            "evaluation":
                result

        }), 200

    finally:

        db.close()


# ============================================================
# DASHBOARD
# ============================================================

@app.route(
    "/api/dashboard",
    methods=["GET"]
)
@jwt_required()
def dashboard():

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        strategies = db.query(
            Strategy
        ).filter(
            Strategy.user_id == user_id
        ).all()

        total_strategies = len(
            strategies
        )

        evaluated = [
            strategy
            for strategy in strategies
            if strategy.score is not None
        ]

        average_score = (

            sum(
                strategy.score
                for strategy in evaluated
            )
            / len(evaluated)

            if evaluated
            else 0
        )

        return jsonify({

            "balance":
                state["balance"],

            "progress":
                state["progress"],

            "positions":
                state["positions"],

            "strategies":
                total_strategies,

            "evaluated_strategies":
                len(evaluated),

            "average_strategy_score":
                round(
                    average_score,
                    2
                )

        }), 200

    finally:

        db.close()


# ============================================================
# STOCKS
# ============================================================

@app.route(
    "/api/stocks",
    methods=["GET"]
)
def get_stocks():

    stocks = []
    errors = []

    for symbol in UPSTOX_INSTRUMENTS:
        try:
            stocks.append(
                build_market_stock(symbol)
            )
        except Exception as exc:
            errors.append({
                "symbol": symbol,
                "error": str(exc),
            })

    return jsonify({
        "stocks": stocks,
        "market_status": get_market_status(),
        "source": "upstox",
        "errors": errors,
    }), 200


# ============================================================
# UPSTOX - MARKET STATUS
# ============================================================

@app.route(
    "/api/market/status",
    methods=["GET"]
)
def market_status():

    return jsonify({
        "market": get_market_status(),
        "source": "upstox",
    }), 200


# ============================================================
# UPSTOX - CURRENT MARKET
# ============================================================

@app.route(
    "/api/market/<symbol>",
    methods=["GET"]
)
def market_data(symbol):

    symbol = symbol.upper()

    if not get_instrument_key(symbol):

        return jsonify({
            "error":
                f"Unsupported symbol: {symbol}"
        }), 400

    try:

        stock = build_market_stock(symbol)

        return jsonify({
            "symbol": symbol,
            "name": STOCK_NAMES.get(symbol, symbol),
            "market_status": get_market_status(),
            "source": "upstox",
            "data": stock,
        }), 200

    except Exception as exc:

        return jsonify({
            "error":
                "Unable to retrieve market data",
            "details":
                str(exc)
        }), 502


# ============================================================
# UPSTOX - VARIATION
# ============================================================

@app.route(
    "/api/market/<symbol>/variation",
    methods=["GET"]
)
def market_variation(symbol):

    symbol = symbol.upper()

    if not get_instrument_key(symbol):

        return jsonify({
            "error":
                f"Unsupported symbol: {symbol}"
        }), 400

    try:

        stock = build_market_stock(symbol)

        return jsonify({
            "status": "success",
            "source": "upstox",
            "market_status": get_market_status(),
            "data": stock,
        }), 200

    except Exception as exc:

        return jsonify({
            "error":
                "Unable to retrieve variation",
            "details":
                str(exc)
        }), 502


# ============================================================
# UPSTOX - HISTORICAL DATA
# ============================================================

@app.route(
    "/api/market/<symbol>/history",
    methods=["GET"]
)
def market_history(symbol):

    symbol = symbol.upper()

    instrument_key = get_instrument_key(
        symbol
    )

    if not instrument_key:

        return jsonify({
            "error":
                f"Unsupported symbol: {symbol}"
        }), 400

    try:

        days = int(
            request.args.get(
                "days",
                365
            )
        )

    except ValueError:

        return jsonify({
            "error":
                "days must be an integer"
        }), 400

    days = max(
        1,
        min(
            days,
            365
        )
    )

    to_date = date.today()

    from_date = (
        to_date
        - timedelta(days=days)
    )

    url = (
        f"{UPSTOX_BASE_URL}"
        f"/v3/historical-candle/"
        f"{instrument_key}/days/1/"
        f"{to_date.isoformat()}/"
        f"{from_date.isoformat()}"
    )

    try:

        response = requests.get(
            url,
            headers=upstox_headers(),
            timeout=15
        )

        if response.status_code != 200:

            return jsonify({

                "error":
                    "Upstox historical request failed",

                "status":
                    response.status_code,

                "details":
                    response.text

            }), 502

        payload = response.json()

        candles = parse_upstox_candles(
            payload
        )

        return jsonify({

            "symbol":
                symbol,

            "days":
                days,

            "candles":
                candles

        }), 200

    except Exception as exc:

        return jsonify({

            "error":
                "Unable to retrieve historical data",

            "details":
                str(exc)

        }), 500


# ============================================================
# MODULE 5 - INDICATORS
# ============================================================

def calculate_sma(
    values,
    period
):

    if len(values) < period:

        return None

    return (
        sum(values[-period:])
        / period
    )


def calculate_ema(
    values,
    period
):

    if len(values) < period:

        return None

    multiplier = (
        2 / (period + 1)
    )

    ema = (
        sum(values[:period])
        / period
    )

    for price in values[period:]:

        ema = (
            (price - ema)
            * multiplier
            + ema
        )

    return ema


def calculate_rsi(
    values,
    period=14
):

    if len(values) <= period:

        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses.append(0)

        else:

            gains.append(0)
            losses.append(
                abs(change)
            )

    recent_gains = (
        gains[-period:]
    )

    recent_losses = (
        losses[-period:]
    )

    average_gain = (
        sum(recent_gains)
        / period
    )

    average_loss = (
        sum(recent_losses)
        / period
    )

    if average_loss == 0:

        return 100.0

    rs = (
        average_gain
        / average_loss
    )

    return (
        100
        - (
            100
            / (1 + rs)
        )
    )


# ============================================================
# MODULE 5 - ENTRY RULE PARSER
# ============================================================

def parse_entry_rule(
    entry_rule
):

    text = (
        entry_rule
        .lower()
        .strip()
    )

    # --------------------------------------------------------
    # PRICE ABOVE
    # --------------------------------------------------------

    match = re.search(
        r"(?:buy|price|enter|entry)?\s*"
        r"above\s*"
        r"(?:rs\.?|₹|\$)?\s*"
        r"(\d+(?:\.\d+)?)",
        text
    )

    if match:

        return {

            "type":
                "price_above",

            "value":
                float(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # PRICE BELOW
    # --------------------------------------------------------

    match = re.search(
        r"(?:buy|price|enter|entry)?\s*"
        r"below\s*"
        r"(?:rs\.?|₹|\$)?\s*"
        r"(\d+(?:\.\d+)?)",
        text
    )

    if match:

        return {

            "type":
                "price_below",

            "value":
                float(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # SMA ABOVE
    # --------------------------------------------------------

    match = re.search(
        r"(?:above|buy above|cross above)\s*"
        r"(?:the\s*)?"
        r"sma\s*(\d+)",
        text
    )

    if match:

        return {

            "type":
                "sma_above",

            "period":
                int(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # SMA BELOW
    # --------------------------------------------------------

    match = re.search(
        r"(?:below|buy below|cross below)\s*"
        r"(?:the\s*)?"
        r"sma\s*(\d+)",
        text
    )

    if match:

        return {

            "type":
                "sma_below",

            "period":
                int(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # EMA ABOVE
    # --------------------------------------------------------

    match = re.search(
        r"(?:above|buy above|cross above)\s*"
        r"(?:the\s*)?"
        r"ema\s*(\d+)",
        text
    )

    if match:

        return {

            "type":
                "ema_above",

            "period":
                int(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # EMA BELOW
    # --------------------------------------------------------

    match = re.search(
        r"(?:below|buy below|cross below)\s*"
        r"(?:the\s*)?"
        r"ema\s*(\d+)",
        text
    )

    if match:

        return {

            "type":
                "ema_below",

            "period":
                int(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # RSI BELOW
    # --------------------------------------------------------

    match = re.search(
        r"rsi\s*"
        r"(?:below|under|less than)\s*"
        r"(\d+)",
        text
    )

    if match:

        return {

            "type":
                "rsi_below",

            "value":
                float(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # RSI ABOVE
    # --------------------------------------------------------

    match = re.search(
        r"rsi\s*"
        r"(?:above|over|greater than)\s*"
        r"(\d+)",
        text
    )

    if match:

        return {

            "type":
                "rsi_above",

            "value":
                float(
                    match.group(1)
                )

        }

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    if "breakout" in text:

        return {
            "type":
                "breakout"
        }

    return None


# ============================================================
# MODULE 5 - EXIT RULE PARSER
# ============================================================

def parse_exit_rule(
    exit_rule
):

    text = (
        exit_rule
        .lower()
        .strip()
    )

    result = {}

    # --------------------------------------------------------
    # TARGET
    # --------------------------------------------------------

    target_match = re.search(
        r"(?:target|take profit|profit)"
        r"\s*(?:of\s*)?"
        r"(\d+(?:\.\d+)?)"
        r"\s*%",
        text
    )

    if not target_match:

        target_match = re.search(
            r"(\d+(?:\.\d+)?)"
            r"\s*%"
            r"\s*(?:target|profit)",
            text
        )

    if target_match:

        result[
            "target_percent"
        ] = float(
            target_match.group(1)
        )

    # --------------------------------------------------------
    # STOP LOSS
    # --------------------------------------------------------

    stop_match = re.search(
        r"(?:stop\s*loss|sl)"
        r"\s*(?:of\s*)?"
        r"(\d+(?:\.\d+)?)"
        r"\s*%",
        text
    )

    if not stop_match:

        stop_match = re.search(
            r"(\d+(?:\.\d+)?)"
            r"\s*%\s*stop",
            text
        )

    if stop_match:

        result[
            "stop_loss_percent"
        ] = float(
            stop_match.group(1)
        )

    return result


# ============================================================
# MODULE 5 - TIME HORIZON
# ============================================================

def parse_time_horizon(
    time_horizon
):

    text = (
        time_horizon
        .lower()
    )

    match = re.search(
        r"(\d+)\s*"
        r"(?:day|days|d)",
        text
    )

    if match:

        return int(
            match.group(1)
        )

    match = re.search(
        r"(\d+)\s*"
        r"(?:week|weeks|w)",
        text
    )

    if match:

        return (
            int(
                match.group(1)
            )
            * 7
        )

    match = re.search(
        r"(\d+)\s*"
        r"(?:month|months|m)",
        text
    )

    if match:

        return (
            int(
                match.group(1)
            )
            * 30
        )

    return None


# ============================================================
# MODULE 5 - ENTRY SIGNAL
# ============================================================

def check_entry_signal(
    entry_rule,
    closes,
    highs,
    lows
):

    rule_type = (
        entry_rule["type"]
    )

    current_price = closes[-1]

    # --------------------------------------------------------
    # PRICE ABOVE
    # --------------------------------------------------------

    if rule_type == "price_above":

        return (
            current_price
            > entry_rule["value"]
        )

    # --------------------------------------------------------
    # PRICE BELOW
    # --------------------------------------------------------

    if rule_type == "price_below":

        return (
            current_price
            < entry_rule["value"]
        )

    # --------------------------------------------------------
    # SMA ABOVE
    # --------------------------------------------------------

    if rule_type == "sma_above":

        period = (
            entry_rule["period"]
        )

        sma = calculate_sma(
            closes,
            period
        )

        if sma is None:

            return False

        return (
            current_price > sma
        )

    # --------------------------------------------------------
    # SMA BELOW
    # --------------------------------------------------------

    if rule_type == "sma_below":

        period = (
            entry_rule["period"]
        )

        sma = calculate_sma(
            closes,
            period
        )

        if sma is None:

            return False

        return (
            current_price < sma
        )

    # --------------------------------------------------------
    # EMA ABOVE
    # --------------------------------------------------------

    if rule_type == "ema_above":

        period = (
            entry_rule["period"]
        )

        ema = calculate_ema(
            closes,
            period
        )

        if ema is None:

            return False

        return (
            current_price > ema
        )

    # --------------------------------------------------------
    # EMA BELOW
    # --------------------------------------------------------

    if rule_type == "ema_below":

        period = (
            entry_rule["period"]
        )

        ema = calculate_ema(
            closes,
            period
        )

        if ema is None:

            return False

        return (
            current_price < ema
        )

    # --------------------------------------------------------
    # RSI BELOW
    # --------------------------------------------------------

    if rule_type == "rsi_below":

        rsi = calculate_rsi(
            closes
        )

        if rsi is None:

            return False

        return (
            rsi < entry_rule["value"]
        )

    # --------------------------------------------------------
    # RSI ABOVE
    # --------------------------------------------------------

    if rule_type == "rsi_above":

        rsi = calculate_rsi(
            closes
        )

        if rsi is None:

            return False

        return (
            rsi > entry_rule["value"]
        )

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    if rule_type == "breakout":

        if len(highs) < 21:

            return False

        previous_highs = (
            highs[-21:-1]
        )

        highest_previous_price = max(
            previous_highs
        )

        return (
            current_price
            > highest_previous_price
        )

    return False


# ============================================================
# MODULE 5 - BACKTEST ENGINE
# ============================================================

def run_backtest(
    candles,
    entry_rule,
    exit_rule,
    time_horizon_days=None,
    initial_capital=100000.0
):

    if len(candles) < 30:

        raise ValueError(
            "Not enough historical candles for backtesting."
        )

    cash = float(
        initial_capital
    )

    position = None

    trades = []

    equity_curve = []

    max_equity = (
        initial_capital
    )

    max_drawdown = 0.0

    target_percent = (
        exit_rule.get(
            "target_percent"
        )
    )

    stop_loss_percent = (
        exit_rule.get(
            "stop_loss_percent"
        )
    )

    closes = []
    highs = []
    lows = []

    for i, candle in enumerate(
        candles
    ):

        candle_open = float(
            candle["open"]
        )

        candle_high = float(
            candle["high"]
        )

        candle_low = float(
            candle["low"]
        )

        candle_close = float(
            candle["close"]
        )

        closes.append(
            candle_close
        )

        highs.append(
            candle_high
        )

        lows.append(
            candle_low
        )

        # ====================================================
        # MANAGE EXISTING POSITION
        # ====================================================

        if position is not None:

            entry_price = (
                position["entry_price"]
            )

            quantity = (
                position["quantity"]
            )

            target_price = None
            stop_price = None

            if target_percent is not None:

                target_price = (
                    entry_price
                    * (
                        1
                        + target_percent / 100
                    )
                )

            if stop_loss_percent is not None:

                stop_price = (
                    entry_price
                    * (
                        1
                        - stop_loss_percent / 100
                    )
                )

            exit_price = None
            exit_reason = None

            # ------------------------------------------------
            # STOP LOSS
            # ------------------------------------------------

            if (
                stop_price is not None
                and candle_low <= stop_price
            ):

                exit_price = (
                    stop_price
                )

                exit_reason = (
                    "STOP_LOSS"
                )

            # ------------------------------------------------
            # TARGET
            # ------------------------------------------------

            elif (
                target_price is not None
                and candle_high >= target_price
            ):

                exit_price = (
                    target_price
                )

                exit_reason = (
                    "TARGET"
                )

            # ------------------------------------------------
            # TIME EXIT
            # ------------------------------------------------

            elif (
                time_horizon_days
                is not None
                and (
                    i
                    - position["entry_index"]
                    >= time_horizon_days
                )
            ):

                exit_price = (
                    candle_close
                )

                exit_reason = (
                    "TIME_EXIT"
                )

            # ------------------------------------------------
            # EXIT
            # ------------------------------------------------

            if exit_price is not None:

                proceeds = (
                    quantity
                    * exit_price
                )

                profit = (
                    exit_price
                    - entry_price
                ) * quantity

                cash += proceeds

                invested_amount = (
                    entry_price
                    * quantity
                )

                trade_return = (

                    profit
                    / invested_amount

                ) * 100

                trades.append({

                    "entry_price":
                        round(
                            entry_price,
                            2
                        ),

                    "exit_price":
                        round(
                            exit_price,
                            2
                        ),

                    "quantity":
                        quantity,

                    "profit":
                        round(
                            profit,
                            2
                        ),

                    "return_percent":
                        round(
                            trade_return,
                            2
                        ),

                    "reason":
                        exit_reason

                })

                position = None

        # ====================================================
        # ENTRY SIGNAL
        # ====================================================

        if (
            position is None
            and i < len(candles) - 1
        ):

            signal = (
                check_entry_signal(
                    entry_rule,
                    closes,
                    highs,
                    lows
                )
            )

            if signal:

                next_open = float(
                    candles[i + 1]["open"]
                )

                # Use 95% of available capital.
                investment = (
                    cash * 0.95
                )

                quantity = int(
                    investment
                    / next_open
                )

                if quantity > 0:

                    cost = (
                        quantity
                        * next_open
                    )

                    cash -= cost

                    position = {

                        "entry_price":
                            next_open,

                        "quantity":
                            quantity,

                        "entry_index":
                            i + 1

                    }

        # ====================================================
        # EQUITY
        # ====================================================

        if position is not None:

            current_equity = (
                cash
                + (
                    position["quantity"]
                    * candle_close
                )
            )

        else:

            current_equity = cash

        equity_curve.append(
            current_equity
        )

        # ====================================================
        # DRAWDOWN
        # ====================================================

        if current_equity > max_equity:

            max_equity = (
                current_equity
            )

        if max_equity > 0:

            drawdown = (

                (
                    max_equity
                    - current_equity
                )
                / max_equity

            ) * 100

            max_drawdown = max(
                max_drawdown,
                drawdown
            )

    # ========================================================
    # CLOSE OPEN POSITION AT END OF DATA
    # ========================================================

    if position is not None:

        final_price = float(
            candles[-1]["close"]
        )

        proceeds = (
            position["quantity"]
            * final_price
        )

        profit = (

            final_price
            - position["entry_price"]

        ) * position["quantity"]

        cash += proceeds

        invested_amount = (

            position["entry_price"]
            * position["quantity"]

        )

        trade_return = (

            profit
            / invested_amount

        ) * 100

        trades.append({

            "entry_price":
                round(
                    position["entry_price"],
                    2
                ),

            "exit_price":
                round(
                    final_price,
                    2
                ),

            "quantity":
                position["quantity"],

            "profit":
                round(
                    profit,
                    2
                ),

            "return_percent":
                round(
                    trade_return,
                    2
                ),

            "reason":
                "END_OF_DATA"

        })

    # ========================================================
    # METRICS
    # ========================================================

    final_capital = cash

    total_return = (

        (
            final_capital
            - initial_capital
        )
        / initial_capital

    ) * 100

    total_trades = len(
        trades
    )

    winning_trades = len([

        trade
        for trade in trades
        if trade["profit"] > 0

    ])

    losing_trades = len([

        trade
        for trade in trades
        if trade["profit"] <= 0

    ])

    if total_trades > 0:

        win_rate = (

            winning_trades
            / total_trades

        ) * 100

    else:

        win_rate = 0.0

    gross_profit = sum([

        trade["profit"]
        for trade in trades
        if trade["profit"] > 0

    ])

    gross_loss = abs(sum([

        trade["profit"]
        for trade in trades
        if trade["profit"] < 0

    ]))

    if gross_loss > 0:

        profit_factor = (
            gross_profit
            / gross_loss
        )

    elif gross_profit > 0:

        profit_factor = 999.0

    else:

        profit_factor = 0.0

    # ========================================================
    # BACKTEST SCORE
    # ========================================================

    # Return component: maximum 40 points
    return_score = max(
        0,
        min(
            40,
            total_return * 2
        )
    )

    # Win-rate component: maximum 25 points
    win_score = min(
        25,
        win_rate * 0.25
    )

    # Drawdown component: maximum 20 points
    drawdown_score = max(
        0,
        20
        - (
            max_drawdown * 0.5
        )
    )

    # Profit-factor component:
    # maximum 15 points

    if profit_factor >= 2:

        profit_factor_score = 15

    elif profit_factor >= 1.5:

        profit_factor_score = 12

    elif profit_factor >= 1:

        profit_factor_score = 8

    else:

        profit_factor_score = 3

    backtest_score = (

        return_score
        + win_score
        + drawdown_score
        + profit_factor_score

    )

    backtest_score = max(
        0,
        min(
            100,
            backtest_score
        )
    )

    return {

        "initial_capital":
            round(
                initial_capital,
                2
            ),

        "final_capital":
            round(
                final_capital,
                2
            ),

        "total_return":
            round(
                total_return,
                2
            ),

        "total_trades":
            total_trades,

        "winning_trades":
            winning_trades,

        "losing_trades":
            losing_trades,

        "win_rate":
            round(
                win_rate,
                2
            ),

        "max_drawdown":
            round(
                max_drawdown,
                2
            ),

        "profit_factor":
            round(
                profit_factor,
                2
            ),

        "backtest_score":
            round(
                backtest_score,
                2
            ),

        "trades":
            trades
    }


# ============================================================
# MODULE 5 - RUN BACKTEST
# ============================================================

@app.route(
    "/api/strategies/<int:strategy_id>/backtest",
    methods=["POST"]
)
@jwt_required()
def backtest_strategy(
    strategy_id
):

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        # ====================================================
        # FIND STRATEGY
        # ====================================================

        strategy = db.query(
            Strategy
        ).filter(
            Strategy.id == strategy_id,
            Strategy.user_id == user_id
        ).first()

        if strategy is None:

            return jsonify({
                "error":
                    "Strategy not found"
            }), 404

        # ====================================================
        # PARSE ENTRY
        # ====================================================

        parsed_entry = (
            parse_entry_rule(
                strategy.entry_rule
            )
        )

        if parsed_entry is None:

            return jsonify({

                "error":
                    "Entry rule cannot be backtested",

                "supported_examples": [

                    "buy above 2500",

                    "buy below 2500",

                    "above SMA 20",

                    "below SMA 20",

                    "above EMA 20",

                    "below EMA 20",

                    "RSI below 30",

                    "RSI above 70",

                    "breakout"

                ]

            }), 400

        # ====================================================
        # PARSE EXIT
        # ====================================================

        parsed_exit = (
            parse_exit_rule(
                strategy.exit_rule
            )
        )

        time_horizon_days = (
            parse_time_horizon(
                strategy.time_horizon
            )
        )

        if (

            not parsed_exit.get(
                "target_percent"
            )

            and not parsed_exit.get(
                "stop_loss_percent"
            )

            and time_horizon_days is None

        ):

            return jsonify({

                "error":
                    "Exit rule cannot be backtested",

                "supported_examples": [

                    "10% target",

                    "5% stop loss",

                    "10% target and 5% stop loss"

                ]

            }), 400

        # ====================================================
        # REQUEST DATA
        # ====================================================

        data = request.get_json(
            silent=True
        ) or {}

        try:

            initial_capital = float(
                data.get(
                    "initial_capital",
                    100000
                )
            )

        except (
            TypeError,
            ValueError
        ):

            return jsonify({
                "error":
                    "initial_capital must be a number"
            }), 400

        if initial_capital <= 0:

            return jsonify({
                "error":
                    "Initial capital must be greater than 0"
            }), 400

        # ====================================================
        # INSTRUMENT
        # ====================================================

        symbol = (
            strategy.asset_symbol
            .upper()
        )

        instrument_key = (
            get_instrument_key(
                symbol
            )
        )

        if instrument_key is None:

            return jsonify({

                "error":
                    "No Upstox instrument mapping exists",

                "symbol":
                    symbol,

                "supported_symbols":
                    list(
                        UPSTOX_INSTRUMENTS.keys()
                    )

            }), 400

        # ====================================================
        # HISTORICAL DATA
        # ====================================================

        to_date = date.today()

        from_date = (
            to_date
            - timedelta(
                days=365
            )
        )

        url = (

            f"{UPSTOX_BASE_URL}"
            f"/v3/historical-candle/"
            f"{instrument_key}"
            f"/days/1/"
            f"{to_date.isoformat()}/"
            f"{from_date.isoformat()}"

        )

        response = requests.get(

            url,

            headers=
                upstox_headers(),

            timeout=15

        )

        if response.status_code != 200:

            return jsonify({

                "error":
                    "Unable to retrieve Upstox historical data",

                "upstox_status":
                    response.status_code,

                "details":
                    response.text

            }), 502

        payload = (
            response.json()
        )

        candles = (
            parse_upstox_candles(
                payload
            )
        )

        if len(candles) < 30:

            return jsonify({

                "error":
                    "Not enough historical data for backtesting",

                "candle_count":
                    len(candles)

            }), 400

        # ====================================================
        # RUN ENGINE
        # ====================================================

        result = run_backtest(

            candles=candles,

            # pyrefly: ignore [unexpected-keyword]
            entry_rule=parsed_entry,

            # pyrefly: ignore [unexpected-keyword]
            exit_rule=parsed_exit,

            # pyrefly: ignore [unexpected-keyword]
            time_horizon_days=
                time_horizon_days,

            initial_capital=
                initial_capital

        )

        # ====================================================
        # SAVE RESULT
        # ====================================================

        backtest = BacktestResult(

            strategy_id=
                strategy.id,

            initial_capital=
                result[
                    "initial_capital"
                ],

            final_capital=
                result[
                    "final_capital"
                ],

            total_return=
                result[
                    "total_return"
                ],

            total_trades=
                result[
                    "total_trades"
                ],

            winning_trades=
                result[
                    "winning_trades"
                ],

            losing_trades=
                result[
                    "losing_trades"
                ],

            win_rate=
                result[
                    "win_rate"
                ],

            max_drawdown=
                result[
                    "max_drawdown"
                ],

            profit_factor=
                result[
                    "profit_factor"
                ],

            backtest_score=
                result[
                    "backtest_score"
                ]

        )

        db.add(
            backtest
        )

        db.commit()

        db.refresh(
            backtest
        )

        # ====================================================
        # RESPONSE
        # ====================================================

        return jsonify({

            "message":
                "Backtest completed",

            "backtest_id":
                backtest.id,

            "strategy_id":
                strategy.id,

            "symbol":
                symbol,

            "entry_rule":
                strategy.entry_rule,

            "exit_rule":
                strategy.exit_rule,

            "time_horizon":
                strategy.time_horizon,

            "parsed_entry_rule":
                parsed_entry,

            "parsed_exit_rule":
                parsed_exit,

            "results":
                result

        }), 200

    except ValueError as exc:

        db.rollback()

        return jsonify({

            "error":
                str(exc)

        }), 400

    except Exception as exc:

        db.rollback()

        return jsonify({

            "error":
                "Backtest failed",

            "details":
                str(exc)

        }), 500

    finally:

        db.close()


# ============================================================
# MODULE 5 - BACKTEST HISTORY
# ============================================================

@app.route(
    "/api/strategies/<int:strategy_id>/backtests",
    methods=["GET"]
)
@jwt_required()
def get_backtests(
    strategy_id
):

    user_id = int(
        get_jwt_identity()
    )

    db = SessionLocal()

    try:

        strategy = db.query(
            Strategy
        ).filter(
            Strategy.id == strategy_id,
            Strategy.user_id == user_id
        ).first()

        if strategy is None:

            return jsonify({
                "error":
                    "Strategy not found"
            }), 404

        results = db.query(
            BacktestResult
        ).filter(
            BacktestResult.strategy_id
            == strategy_id
        ).order_by(
            BacktestResult.created_at.desc()
        ).all()

        return jsonify({

            "strategy_id":
                strategy_id,

            "backtests": [

                {

                    "id":
                        result.id,

                    "initial_capital":
                        result.initial_capital,

                    "final_capital":
                        result.final_capital,

                    "total_return":
                        result.total_return,

                    "total_trades":
                        result.total_trades,

                    "winning_trades":
                        result.winning_trades,

                    "losing_trades":
                        result.losing_trades,

                    "win_rate":
                        result.win_rate,

                    "max_drawdown":
                        result.max_drawdown,

                    "profit_factor":
                        result.profit_factor,

                    "backtest_score":
                        result.backtest_score,

                    "created_at":
                        (
                            result.created_at.isoformat()
                            if result.created_at
                            else None
                        )

                }

                for result in results

            ]

        }), 200

    finally:

        db.close()


# ============================================================
# LEARNING PROGRESS
# ============================================================

@app.route(
    "/api/learning/progress",
    methods=["GET"]
)
@jwt_required()
def learning_progress():

    return jsonify({

        "progress":
            state["progress"],

        "completed_modules": [

            "Market Basics",
            "Risk Management",
            "Strategy Building"

        ]

    }), 200


# ============================================================
# SIMULATOR
# ============================================================

@app.route(
    "/api/simulator/trade",
    methods=["POST"]
)
@jwt_required()
def simulator_trade():

    data = request.get_json(
        silent=True
    ) or {}

    symbol = str(
        data.get("symbol", "")
    ).upper()

    quantity = data.get(
        "quantity",
        0
    )

    side = str(
        data.get("side", "BUY")
    ).upper()

    try:

        quantity = int(
            quantity
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "error":
                "Quantity must be an integer"
        }), 400

    if not symbol:

        return jsonify({
            "error":
                "Symbol is required"
        }), 400

    if quantity <= 0:

        return jsonify({
            "error":
                "Quantity must be greater than zero"
        }), 400

    if side not in [
        "BUY",
        "SELL"
    ]:

        return jsonify({
            "error":
                "Side must be BUY or SELL"
        }), 400

    with state_lock:

        current_quantity = (
            state["positions"]
            .get(
                symbol,
                0
            )
        )

        if side == "BUY":

            state["positions"][
                symbol
            ] = (
                current_quantity
                + quantity
            )

        else:

            if quantity > current_quantity:

                return jsonify({
                    "error":
                        "Not enough shares to sell"
                }), 400

            state["positions"][
                symbol
            ] = (
                current_quantity
                - quantity
            )

        return jsonify({

            "message":
                "Trade simulated",

            "symbol":
                symbol,

            "side":
                side,

            "quantity":
                quantity,

            "positions":
                state["positions"]

        }), 200


# ============================================================
# AI MENTOR
# ============================================================

@app.route(
    "/api/mentor",
    methods=["POST"]
)
@jwt_required()
def mentor():

    data = request.get_json(
        silent=True
    ) or {}

    question = str(
        data.get("question", "")
    ).strip()

    if not question:

        return jsonify({
            "error":
                "Question is required"
        }), 400

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:

        return jsonify({
            "error":
                "OPENAI_API_KEY is not configured"
        }), 500

    # Keep requests reasonably small so the AI Advisor is predictable
    # and does not receive unnecessarily large prompts.
    if len(question) > 4000:

        return jsonify({
            "error":
                "Question is too long. Please keep it under 4000 characters."
        }), 400

    try:

        client = OpenAI(
            api_key=api_key
        )

        response = client.responses.create(

            model=os.getenv(
                "OPENAI_MODEL",
                "gpt-5.6-luna"
            ),

            instructions=(
                "You are the AI Advisor inside an educational stock "
                "strategy learning application called AI Strategy Judge. "
                "Answer the user's question clearly and in beginner-friendly "
                "language. You can explain stock-market concepts, technical "
                "indicators, strategies, risk management, backtesting, and "
                "trading concepts. Do not guarantee profits or present "
                "uncertain predictions as facts. Do not claim to know live "
                "market data unless it is explicitly provided in the prompt. "
                "Keep answers practical and structured. This is educational "
                "information, not personalized financial advice."
            ),

            input=question
        )

        answer = (
            response.output_text
            if response.output_text
            else "I could not generate a response right now."
        )

        return jsonify({

            "question":
                question,

            "answer":
                answer

        }), 200

    except Exception as exc:

        print(
            "AI Advisor error:",
            exc
        )

        return jsonify({

            "error":
                "Unable to generate AI response right now."

        }), 500


# ============================================================
# JWT ERROR HANDLERS
# ============================================================

@jwt.unauthorized_loader
def unauthorized_loader(error):

    return jsonify({

        "error":
            "Authorization token is required",

        "details":
            error

    }), 401


@jwt.invalid_token_loader
def invalid_token_loader(error):

    return jsonify({

        "error":
            "Invalid authorization token",

        "details":
            error

    }), 401


@jwt.expired_token_loader
def expired_token_loader(
    jwt_header,
    jwt_payload
):

    return jsonify({

        "error":
            "Authorization token has expired"

    }), 401


# ============================================================
# 404
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify({

        "error":
            "Endpoint not found"

    }), 404


# ============================================================
# 500
# ============================================================

@app.errorhandler(500)
def internal_error(error):

    return jsonify({

        "error":
            "Internal server error"

    }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True

    )

