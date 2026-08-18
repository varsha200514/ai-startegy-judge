from dataclasses import asdict, dataclass
from datetime import date
from threading import Lock
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

from database import engine, Base, SessionLocal
from models import Stock as StockModel

# Create database tables if they don't already exist
Base.metadata.create_all(bind=engine)

app = Flask(__name__)
CORS(app)

RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "INR": 83.1
}


# --------------------------------------------------
# Existing Stock data used by your dashboard/simulator
# --------------------------------------------------

@dataclass(frozen=True)
class Stock:
    symbol: str
    name: str
    price: float
    change: float
    points: str


STOCKS = [
    Stock(
        "AAPL",
        "Apple Inc.",
        189.98,
        2.43,
        "2,12 18,16 34,10 50,15 66,7 82,9 98,3"
    ),
    Stock(
        "TSLA",
        "Tesla, Inc.",
        248.76,
        -1.28,
        "2,5 18,9 34,7 50,14 66,10 82,17 98,13"
    ),
    Stock(
        "NVDA",
        "NVIDIA Corp.",
        485.92,
        4.81,
        "2,16 18,13 34,15 50,8 66,11 82,4 98,7"
    ),
    Stock(
        "BTC",
        "Bitcoin",
        68420.0,
        3.16,
        "2,15 18,11 34,14 50,5 66,9 82,3 98,6"
    ),
]


# --------------------------------------------------
# Application state
# --------------------------------------------------

state: dict[str, Any] = {
    "balance": 24850.42,
    "progress": 68,
    "positions": {
        "AAPL": 12,
        "NVDA": 8
    },
}

state_lock = Lock()


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def convert(value: float, currency: str) -> float:
    return round(
        value * RATES.get(currency.upper(), 1.0),
        2
    )


def stock_payload(currency: str) -> list[dict[str, Any]]:
    return [
        {
            **asdict(stock),
            "converted_price": convert(stock.price, currency)
        }
        for stock in STOCKS
    ]


# --------------------------------------------------
# Health API
# --------------------------------------------------

@app.get("/api/health")
def health() -> Any:
    return jsonify({
        "status": "ok",
        "service": "ai-strategy-judge",
        "date": date.today().isoformat()
    })


# --------------------------------------------------
# Dashboard API
# --------------------------------------------------

@app.get("/api/dashboard")
def dashboard() -> Any:
    currency = request.args.get(
        "currency",
        "USD"
    ).upper()

    query = request.args.get(
        "q",
        ""
    ).strip().lower()

    stocks = [
        stock
        for stock in stock_payload(currency)
        if not query
        or query in f"{stock['symbol']} {stock['name']}".lower()
    ]

    with state_lock:
        snapshot = {
            "balance": convert(
                state["balance"],
                currency
            ),
            "progress": state["progress"],
            "positions": dict(
                state["positions"]
            )
        }

    return jsonify({
        "currency": currency if currency in RATES else "USD",
        "rates": RATES,
        "portfolio": snapshot,
        "stocks": stocks,
        "recommendations": [
            {
                "type": "BUY",
                "symbol": "NVDA",
                "text": "Momentum remains strong",
                "confidence": 94
            },
            {
                "type": "HOLD",
                "symbol": "AAPL",
                "text": "Healthy consolidation zone",
                "confidence": 82
            },
            {
                "type": "SELL",
                "symbol": "TSLA",
                "text": "Volatility risk elevated",
                "confidence": 76
            }
        ]
    })


# --------------------------------------------------
# DATABASE STOCK API
# --------------------------------------------------

@app.get("/api/stocks")
def get_stocks():
    db = SessionLocal()

    try:
        stocks = db.query(StockModel).all()

        return jsonify([
            {
                "id": stock.id,
                "symbol": stock.symbol,
                "company_name": stock.company_name,
                "exchange": stock.exchange,
                "price": stock.price
            }
            for stock in stocks
        ])

    finally:
        db.close()


# --------------------------------------------------
# Learning Progress API
# --------------------------------------------------

@app.post("/api/learning/progress")
def update_progress() -> Any:
    payload = request.get_json(
        silent=True
    ) or {}

    increment = max(
        0,
        min(
            int(
                payload.get(
                    "increment",
                    8
                )
            ),
            32
        )
    )

    with state_lock:
        state["progress"] = min(
            100,
            state["progress"] + increment
        )

        return jsonify({
            "progress": state["progress"]
        })


# --------------------------------------------------
# Trading Simulator API
# --------------------------------------------------

@app.post("/api/simulator/trade")
def simulator_trade() -> Any:
    payload = request.get_json(
        silent=True
    ) or {}

    symbol = str(
        payload.get(
            "symbol",
            ""
        )
    ).upper()

    side = str(
        payload.get(
            "side",
            ""
        )
    ).lower()

    quantity = payload.get(
        "quantity",
        1
    )

    if (
        symbol not in {
            stock.symbol
            for stock in STOCKS
        }
        or side not in {
            "buy",
            "sell"
        }
    ):
        return jsonify({
            "error": "Use a valid symbol and buy or sell side."
        }), 400

    try:
        quantity = int(quantity)

    except (TypeError, ValueError):
        return jsonify({
            "error": "Quantity must be a positive integer."
        }), 400

    if quantity <= 0 or quantity > 1000:
        return jsonify({
            "error": "Quantity must be between 1 and 1000."
        }), 400

    stock = next(
        stock
        for stock in STOCKS
        if stock.symbol == symbol
    )

    total = stock.price * quantity

    with state_lock:

        current = state["positions"].get(
            symbol,
            0
        )

        if side == "buy":

            if total > state["balance"]:
                return jsonify({
                    "error": "Insufficient virtual balance."
                }), 400

            state["balance"] -= total

            state["positions"][symbol] = (
                current + quantity
            )

        elif quantity > current:

            return jsonify({
                "error": "Not enough shares to sell."
            }), 400

        else:

            state["balance"] += total

            state["positions"][symbol] = (
                current - quantity
            )

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "total": round(
                total,
                2
            ),
            "balance": round(
                state["balance"],
                2
            ),
            "positions": dict(
                state["positions"]
            )
        })


# --------------------------------------------------
# AI Mentor API
# --------------------------------------------------

@app.post("/api/mentor")
def mentor() -> Any:
    payload = request.get_json(
        silent=True
    ) or {}

    message = str(
        payload.get(
            "message",
            ""
        )
    ).strip()

    if not message:
        return jsonify({
            "error": "Message is required."
        }), 400

    return jsonify({
        "reply": (
            "Start with your risk limit, then let "
            "the strategy earn the right to scale. "
            "This is simulated guidance, not financial advice."
        )
    })


# --------------------------------------------------
# Start Flask Server
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )