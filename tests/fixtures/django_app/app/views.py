from django.contrib.auth.decorators import login_required
from django.views import View
from django.views.decorators.http import require_POST

from .services import create_order_row, load_user


@login_required
def user_detail(request, user_id):
    # auth via decorator -> has_auth True; default method GET
    return load_user(user_id)


@require_POST
def health(request):
    # near-miss shape check: @require_POST pins the method to POST
    return {"ok": True}


def legacy_redirect(request, slug):
    return slug


class OrderList(View):
    # CBV: one route per defined http-method handler
    def get(self, request):
        return []

    def post(self, request):
        # call graph must walk through CBV methods too
        return create_order_row(request)
