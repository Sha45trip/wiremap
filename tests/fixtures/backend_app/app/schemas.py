from pydantic import BaseModel


class ItemBase(BaseModel):
    id: int


class ItemOut(ItemBase):
    name: str
    price: float


class ItemCreate(BaseModel):
    name: str                 # required
    price: float              # required
    note: str = ""            # optional (has default)
    tag: "str | None" = None  # optional (nullable)
