from typing import List

from fastapi import APIRouter

from .schemas import ItemOut

capi = APIRouter(prefix="/contract")


@capi.get("/item")
def item_annotated() -> ItemOut:
    # return annotation -> CERTAIN field set (id inherited, name, price)
    ...


@capi.get("/item2", response_model=ItemOut)
def item_keyword():
    # response_model kwarg -> CERTAIN field set
    ...


@capi.get("/items", response_model=List[ItemOut])
def items_list():
    # near-miss: subscripted model is NOT a certain field set -> no
    # response_fields, contract checking must stay silent
    ...


@capi.get("/raw")
def raw_dict():
    # near-miss: no declared model -> no response_fields
    return {}
