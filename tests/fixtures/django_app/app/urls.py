from django.urls import path, re_path
from rest_framework.routers import DefaultRouter

from . import views
from .views import health
from .viewsets import ItemViewSet

router = DefaultRouter()
router.register("items", ItemViewSet)

urlpatterns = [
    path("users/<int:user_id>/", views.user_detail),
    path("orders/", views.OrderList.as_view()),
    re_path(r"^legacy/(?P<slug>[-\w]+)/$", views.legacy_redirect),
    path("health/", health),
]
