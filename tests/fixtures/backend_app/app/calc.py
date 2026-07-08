from fastapi import APIRouter

from .pricing import compute
from .tax import compute as tax_compute

calc = APIRouter(prefix="/calc")


@calc.get("/price")
def price():
    # resolves through the import map to app.pricing.compute
    return compute(1)


@calc.get("/tax")
def tax_amount():
    # aliased import resolves to app.tax.compute
    return tax_compute(1)
