from django.urls import path
from .views import livekit_ping, livekit_list_rooms, s3_ping
from .webhooks import egress_webhook

app_name = "livekit"

urlpatterns = [
    path("ping/", livekit_ping, name="ping"),
    path("rooms/", livekit_list_rooms, name="list-rooms"),
    path("s3/ping/", s3_ping, name="s3-ping"),
    path("webhooks/egress/", egress_webhook, name="egress-webhook"),
]