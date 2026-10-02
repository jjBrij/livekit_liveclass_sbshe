from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.core.urls")),
    path("api/classes/", include("apps.classes.urls")),
    path("api/livekit/", include("apps.livekit.urls")),

]