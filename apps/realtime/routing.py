from django.urls import re_path

from .consumers import ClassConsumer

websocket_urlpatterns = [
    re_path(r"^ws/classes/(?P<class_id>\d+)/$", ClassConsumer.as_asgi()),
]