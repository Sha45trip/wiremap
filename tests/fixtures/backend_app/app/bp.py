from flask import Blueprint

bp = Blueprint("inventory", __name__, url_prefix="/inv")


@bp.route("/items", methods=["GET", "POST"])
def inv_items():
    # blueprint url_prefix applies; one endpoint per listed method
    return []


def _scoped(rule):
    return "/scoped" + rule


@bp.route(_scoped("/dyn"))
def dyn_route():
    # near-miss: computed path -> no endpoint (never fabricate "/")
    return []
