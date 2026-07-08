from pydantic import BaseModel


class OrderOut(BaseModel):
    id: int
    total: float
