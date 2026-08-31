# pyrefly: ignore [missing-import]
from sqlalchemy import Column, Integer, String, Float
from database import Base


class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, index=True)

    symbol = Column(
        String,
        unique=True,
        nullable=False
    )

    name = Column(
        String,
        nullable=False
    )

    price = Column(
        Float,
        nullable=False
    )

    change = Column(
        Float,
        default=0.0
    )

    points = Column(
        String,
        default=""
    )