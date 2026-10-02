from django.urls import path
from .views import health_check, ping, whoami
from apps.classes.views import my_attendance

app_name = "core"

urlpatterns = [
    path("health/", health_check, name="health-check"),
    path("ping/", ping, name="ping"),
    path("whoami/", whoami, name="whoami"),
    path("attendance/me/", my_attendance, name="my-attendance"),
]