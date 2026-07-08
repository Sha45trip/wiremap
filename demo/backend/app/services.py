from .models import Order

def fetch_orders(order_id):
    return db.query(Order).filter_by(id=order_id).all()

def calc_totals(orders):
    total = 0
    for o in orders:
        if o.amount > 0:
            total += o.amount
    return {"total": total}

def log_event(name):
    print(name)
