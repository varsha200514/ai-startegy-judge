from database import SessionLocal
from models import Stock


stocks = [
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 189.98,
        "change": 2.43,
        "points": "2,12 18,16 34,10 50,15 66,7 82,9 98,3",
    },
    {
        "symbol": "TSLA",
        "name": "Tesla, Inc.",
        "price": 248.76,
        "change": -2.18,
        "points": "2,5 18,9 34,7 50,8 66,10 82,4 98,1",
    },
    {
        "symbol": "NVDA",
        "name": "NVIDIA Corp.",
        "price": 485.92,
        "change": 4.81,
        "points": "2,16 18,13 34,15 50,8 66,11 82,4 98,7",
    },
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "price": 68420,
        "change": 3.16,
        "points": "2,15 18,11 34,14 50,5 66,9 82,6",
    },
]


db = SessionLocal()

try:
    for stock_data in stocks:
        stock = (
            db.query(Stock)
            .filter(Stock.symbol == stock_data["symbol"])
            .first()
        )

        if stock:
            stock.name = stock_data["name"]
            stock.price = stock_data["price"]
            stock.change = stock_data["change"]
            stock.points = stock_data["points"]
        else:
            stock = Stock(**stock_data)
            db.add(stock)

    db.commit()

    print("Stocks inserted/updated successfully!")

finally:
    db.close()