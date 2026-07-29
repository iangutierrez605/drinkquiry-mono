from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/game/(?P<code>[A-Za-z0-9]{4,8})/$", consumers.GameConsumer.as_asgi()),
]
