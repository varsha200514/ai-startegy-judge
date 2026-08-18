# pyrefly: ignore [missing-import]
from sqlalchemy import Column, Integer, String, Float
from database import Base


class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, nullable=False)
    company_name = Column(String, nullable=False)
    exchange = Column(String)
    price = Column(Float)