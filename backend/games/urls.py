from django.urls import path

from .views import (
    CellReplaceView,
    ColumnCategoryReplaceView,
    GameAnswerView,
    GameBoardDetailView,
    GameCreateView,
    GameHistoryView,
    GameHostSeatView,
    GameJoinView,
    GameReportView,
    GameStateView,
    TournamentAdvanceView,
    TournamentDetailView,
    TournamentFinishView,
    TournamentListCreateView,
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
    # Handoff #8: §I host-seat recovery, §J3 lobby preview + replace.
    path("games/<str:code>/host-seat/", GameHostSeatView.as_view(), name="game-host-seat"),
    path("games/<str:code>/board/", GameBoardDetailView.as_view(), name="game-board-detail"),
    path("games/<str:code>/cells/<int:cell_id>/replace/", CellReplaceView.as_view(), name="game-cell-replace"),
    # §F (Handoff #16): lobby-only whole-column category swap — the same
    # host-only + ActionError→409 contract as the cell replace above.
    path(
        "games/<str:code>/columns/<int:column_id>/replace/",
        ColumnCategoryReplaceView.as_view(),
        name="game-column-replace",
    ),
    # §I (Handoff #13): tournaments. The collection root registers before its
    # parameterized children per the house rule; because the child segment is
    # <int:pk> (not a free string like <str:code>), no static sibling can be
    # swallowed here — the precedence pin lives with the games/history/ test.
    path("tournaments/", TournamentListCreateView.as_view(), name="tournament-list-create"),
    path("tournaments/<int:pk>/", TournamentDetailView.as_view(), name="tournament-detail"),
    path("tournaments/<int:pk>/finish/", TournamentFinishView.as_view(), name="tournament-finish"),
    path(
        "tournaments/<int:pk>/rounds/<int:round_number>/advance/",
        TournamentAdvanceView.as_view(),
        name="tournament-advance",
    ),
]
