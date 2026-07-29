from django.urls import path

from .views import (
    GameAnswerView,
    GameCreateView,
    GameHistoryView,
    GameJoinView,
    GameReportView,
    GameStateView,
)

urlpatterns = [
    path("games/", GameCreateView.as_view(), name="game-create"),
    # Static path FIRST so it can't be swallowed by the <code> lookup below
    # ("history" would otherwise be treated as a game code) — Handoff #6 §G2.
    path("games/history/", GameHistoryView.as_view(), name="game-history"),
    path("games/<str:code>/", GameStateView.as_view(), name="game-state"),
    path("games/<str:code>/join/", GameJoinView.as_view(), name="game-join"),
    path("games/<str:code>/answer/", GameAnswerView.as_view(), name="game-answer"),
    path("games/<str:code>/report/", GameReportView.as_view(), name="game-report"),
]
