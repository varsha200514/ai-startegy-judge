import os
import requests
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

from database import SessionLocal
from models import Stock

load_dotenv()

TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")

URL = "https://api.upstox.com/v2/market-quote/quotes"

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {TOKEN}"
}


# Indian stocks
STOCKS = {
    "RELIANCE": {
        "instrument_key": "NSE_EQ|INE002A01018",
        "name": "Reliance Industries"
    },
    "TCS": {
        "instrument_key": "NSE_EQ|INE467B01029",
        "name": "Tata Consultancy Services"
    },
    "INFY": {
        "instrument_key": "NSE_EQ|INE009A01021",
        "name": "Infosys"
    },
    "HDFCBANK": {
        "instrument_key": "NSE_EQ|INE040A01034",
        "name": "HDFC Bank"
    },
    "ICICIBANK": {
        "instrument_key": "NSE_EQ|INE090A01021",
        "name": "ICICI Bank"
    }
}


def get_stock_quote(instrument_key):

    params = {
        "instrument_key": instrument_key
    }

    try:
        response = requests.get(
            URL,
            headers=HEADERS,
            params=params,
            timeout=15
        )

        if response.status_code != 200:

            print("Upstox API Error:")
            print(response.text)

            return None

        return response.json()

    except requests.RequestException as error:

        print("Connection error:", error)

        return None


def update_stock_in_database(
    symbol,
    name,
    price,
    change
):

    db = SessionLocal()

    try:

        stock = db.query(Stock).filter(
            Stock.symbol == symbol
        ).first()

        # Stock already exists → UPDATE
        if stock:

            stock.name = name
            stock.price = price
            stock.change = change

            print(f"Updated {symbol}")

        # Stock doesn't exist → ADD
        else:

            new_stock = Stock(
                symbol=symbol,
                name=name,
                price=price,
                change=change,
                points=""
            )

            db.add(new_stock)

            print(f"Added {symbol}")

        db.commit()

    except Exception as error:

        db.rollback()

        print(
            f"Database error for {symbol}: {error}"
        )

    finally:

        db.close()


def fetch_and_update_stocks():

    for symbol, details in STOCKS.items():

        print("\n----------------------")
        print("Fetching:", symbol)

        data = get_stock_quote(
            details["instrument_key"]
        )

        if not data:
            print(f"Could not fetch {symbol}")
            continue

        try:

            # Get the stock data inside "data"
            quote_data = list(
                data["data"].values()
            )[0]

            # Extract real market values
            price = quote_data["last_price"]

            change = quote_data["net_change"]

            print("Price:", price)
            print("Change:", change)

            # Save/update Supabase
            update_stock_in_database(
                symbol=symbol,
                name=details["name"],
                price=price,
                change=change
            )

        except (KeyError, IndexError) as error:

            print(
                f"Error processing {symbol}: {error}"
            )


if __name__ == "__main__":

    fetch_and_update_stocks()