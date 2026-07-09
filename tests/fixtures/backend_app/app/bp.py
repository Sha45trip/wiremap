from flask import Blueprint

bp = Blueprint("inventory", __name__, url_prefix="/inv")


@bp.route("/items", methods=["GET", "POST"])
def inv_items():
    # blueprint url_prefix applies; one endpoint per listed method
    return []
