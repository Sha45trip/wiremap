from pydantic import BaseModel


class ItemBase(BaseModel):
    id: int


class ItemOut(ItemBase):
    name: str
    price: float
