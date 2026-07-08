from fastapi import FastAPI, Depends
from .auth import get_current_user
from .services import fetch_orders, calc_totals, log_event
from .models import User, Order

app = FastAPI()

@app.get("/api/users/{user_id}")
def get_user(user_id: int, user=Depends(get_current_user)):
    result = db.query(User).get(user_id)
    return result

@app.post("/api/orders")
def create_order(payload: dict):
    # NOTE: no auth dependency, no try/except -- both should be flagged
    order = db.execute(f"INSERT INTO orders VALUES ({payload['id']})")
    log_event("order_created")
    return order

@app.get("/api/orders/{order_id}")
def get_order(order_id: int, user=Depends(get_current_user)):
    try:
        orders = fetch_orders(order_id)
        return calc_totals(orders)
    except Exception:
        return {"error": "not found"}

@app.get("/api/reports/summary")
def report_summary(user=Depends(get_current_user)):
    # never called by the frontend -> unused_endpoint flag
    return calc_totals(fetch_orders(None))
