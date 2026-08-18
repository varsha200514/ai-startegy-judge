from database import SessionLocal
from models import Stock


stocks = [
    Stock(
        symbol="AAPL",
        company_name="Apple Inc.",
        exchange="NASDAQ",
        price=220.00
    ),
    Stock(
        symbol="TSLA",
        company_name="Tesla Inc.",
        exchange="NASDAQ",
        price=340.00
    ),
    Stock(
        symbol="NVDA",
        company_name="NVIDIA Corporation",
        exchange="NASDAQ",
        price=180.00
    ),
    Stock(
        symbol="MSFT",
        company_name="Microsoft Corporation",
        exchange="NASDAQ",
        price=500.00
    ),
]


db = SessionLocal()

try:
    for stock in stocks:
        existing_stock = db.query(Stock).filter(
            Stock.symbol == stock.symbol
        ).first()

        if not existing_stock:
            db.add(stock)

    db.commit()
    print("Sample stocks inserted successfully!")

except Exception as e:
    db.rollback()
    print("Error:", e)

finally:
    db.close()