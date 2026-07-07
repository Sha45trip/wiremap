from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
