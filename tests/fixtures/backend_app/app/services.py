from .models import Item


def load_items(item_id):
    return db.query(Item).filter_by(id=item_id).all()
