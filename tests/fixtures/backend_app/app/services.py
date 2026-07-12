from .models import Item


def load_items(item_id):
    return db.query(Item).filter_by(id=item_id).all()


def run_report(clause):
    # 1 hop: caller passes a request param straight into interpolated SQL
    return db.execute(f"SELECT * FROM items WHERE {clause}")


def deep_report(clause):
    # 2 hops: forwards to run_report which builds the SQL
    return run_report(clause)


def safe_report(item_id):
    # near-miss: parameterized query, no interpolation of the param
    return db.execute("SELECT * FROM items WHERE id = :id", {"id": item_id})
