from django.urls import path
from .views import LogsView

urlpatterns = [
    path("", LogsView.as_view(), name="logs"),
]