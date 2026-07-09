def load_user(user_id):
    return {"id": user_id}


def create_order_row(request):
    return db.execute("INSERT INTO orders VALUES (:id)", request)
