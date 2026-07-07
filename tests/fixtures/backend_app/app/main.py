from fastapi import FastAPI, Depends

from .auth import get_current_user
from .services import load_items
from .models import Item

app = FastAPI()


@app.get("/items/{item_id}")
def get_item(item_id: int, user=Depends(get_current_user)):
    # near-miss: I/O is inside try -> must NOT flag no_error_handling
    try:
        return load_items(item_id)
    except Exception:
        return None


@app.post("/items")
def create_item(payload: dict):
    # planted: mutating route without auth -> missing_auth
    # planted: I/O outside any try -> no_error_handling
    # near-miss: parameterized execute -> must NOT flag sql_injection_risk
    return db.execute("INSERT INTO items VALUES (:id)", payload)


@app.delete("/items/{item_id}")
def delete_item(item_id: int, user=Depends(get_current_user)):
    # near-miss: auth dependency present -> must NOT flag missing_auth
    # planted: f-string SQL -> sql_injection_risk
    db.execute(f"DELETE FROM items WHERE id = {item_id}")
    return {"ok": True}


@app.get("/branchy")
def branchy(user=Depends(get_current_user)):
    # planted: cyclomatic complexity > 10 -> high_complexity
    total = 0
    for i in range(20):
        if i > 1:
            total += 1
        if i > 2:
            total += 1
        if i > 3:
            total += 1
        if i > 4:
            total += 1
        if i > 5:
            total += 1
        if i > 6:
            total += 1
        if i > 7:
            total += 1
        if i > 8:
            total += 1
        if i > 9:
            total += 1
        if i > 10:
            total += 1
    return total
