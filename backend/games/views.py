from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404

from accounts.quotas import quota_denial
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Game, GameStatus, Participant, ParticipantRole
from .serializers import (
    BoardDetailCellSerializer,
    BoardDetailSerializer,
    CreateGameSerializer,
    GameHistorySerializer,
    GameReportSerializer,
    GameStateSerializer,
    JoinGameSerializer,
    ParticipantSerializer,
)
from .services import ActionError, create_game, replace_cell_question


class GameCreateView(APIView):
    """POST /api/games/  {mode, categories: [ids...], questions_per_category}

    Creating a game requires an account; the creator becomes the host and
    gets a host participant token for the WebSocket.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        # Plan quota (settings.PLAN_LIMITS; None = unlimited). Structured 403
        # so the frontend can show a friendly limit message + upsell.
        if denial := quota_denial(request.user, "games"):
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        serializer = CreateGameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        game = create_game(
            host=request.user,
            mode=serializer.validated_data["mode"],
            category_ids=serializer.validated_data["categories"],
            questions_per_category=serializer.validated_data["questions_per_category"],
        )
        host_participant = Participant.objects.create(
            game=game,
            name=request.user.display_name or "Host",
            role=ParticipantRole.HOST,
        )
        return Response(
            {
                "game": GameStateSerializer(game, context={"request": request}).data,
                "participant_token": host_participant.token,
            },
            status=status.HTTP_201_CREATED,
        )


class GameJoinView(APIView):
    """POST /api/games/<code>/join/  {name}

    No account required — this is the game/buzzer/<game_id> flow.
    Returns a participant token the client stores (localStorage) so a
    reload rejoins the same seat with score intact.
    """

    permission_classes = (permissions.AllowAny,)

    def post(self, request, code):
        game = get_object_or_404(Game, code=code.upper())
        serializer = JoinGameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # §H1 (Handoff #8): team names are ALL CAPS, normalized server-side
        # BEFORE the uniqueness/create logic so "team a" and "TEAM A" collide
        # as the same name — both the name-taken 400 below and the
        # IntegrityError race path see the normalized value. Rejoin is by
        # token (above), never by name equality, so pre-existing mixed-case
        # seats from old games keep reclaiming fine.
        name = serializer.validated_data["name"].strip().upper()

        # Rejoin: same name + provided token reclaims the seat. Checked BEFORE
        # the §G cap on purpose: that seat already exists, so a reload at a
        # full table must still return 200 (the reload flow cannot break).
        token = request.data.get("participant_token")
        if token:
            participant = Participant.objects.filter(game=game, token=token).first()
            if participant:
                return Response(
                    {
                        "participant": ParticipantSerializer(participant).data,
                        "participant_token": participant.token,
                        "game": GameStateSerializer(game, context={"request": request}).data,
                    }
                )
        try:
            # §G: count + create atomically under a row lock on the Game so two
            # simultaneous joins can't both see 5 players and make it 7. The
            # locked queryset deliberately has NO select_related (the
            # select_for_update + nullable-FK Postgres foot-gun, CHANGES.md).
            # The host's control seat is role=host and never counts.
            with transaction.atomic():
                locked_game = Game.objects.select_for_update().get(pk=game.pk)
                player_count = locked_game.participants.filter(role=ParticipantRole.PLAYER).count()
                if player_count >= settings.MAX_PLAYERS_PER_GAME:
                    # NEW structured contract (separate from the quota_* 403s;
                    # never routed through the quota helpers): exactly
                    # {"detail", "code": "game_full", "limit"} via Response.
                    return Response(
                        {
                            "detail": (
                                f"This game already has {settings.MAX_PLAYERS_PER_GAME} teams — "
                                "join an existing team and share their buzzer."
                            ),
                            "code": "game_full",
                            "limit": settings.MAX_PLAYERS_PER_GAME,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                participant = Participant.objects.create(
                    game=game,
                    name=name,
                    # §I: round-robin sound by join order — teams 1..4 all
                    # sound different, then it wraps.
                    buzzer_sound=(player_count % 4) + 1,
                )
        except IntegrityError:
            # Raised out of the atomic block (so the transaction rolled back
            # cleanly) when the name races unique_participant_name_per_game.
            return Response(
                {"name": ["That name is already taken in this game."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "participant": ParticipantSerializer(participant).data,
                "participant_token": participant.token,
                "game": GameStateSerializer(game, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )


class GameStateView(APIView):
    """GET /api/games/<code>/ — full board snapshot (also pushed over WS)."""

    permission_classes = (permissions.AllowAny,)

    def get(self, request, code):
        game = get_object_or_404(
            Game.objects.prefetch_related("columns__cells", "participants", "columns__category")
            .select_related("current_cell__question", "judged_participant"),
            code=code.upper(),
        )
        return Response(GameStateSerializer(game, context={"request": request}).data)


class GameAnswerView(APIView):
    """GET /api/games/<code>/answer/ — the open cell's answer, HOST ONLY.

    Handoff #6 §F1: the sanctioned pre-reveal side channel so the host can
    judge from /host while the room watches /board. Knox-authenticated
    (participants hold participant tokens, not Knox, so they can't even reach
    the host check — but a second Knox user must still 403). Works regardless
    of reveal state: its whole point is pre-reveal. NOT part of any snapshot;
    rule 5 stays intact.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, code):
        game = get_object_or_404(Game, code=code.upper())
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can see the answer."}, status=status.HTTP_403_FORBIDDEN)
        if game.current_cell_id is None:
            return Response({"detail": "No question is open."}, status=status.HTTP_409_CONFLICT)
        cell = game.current_cell
        return Response({"question_id": cell.question_id, "answer": cell.question.answer})


class GameHostSeatView(APIView):
    """GET /api/games/<code>/host-seat/ — re-issue the host's own seat.

    §I (Handoff #8): the host's participant token normally lives only in the
    browser's localStorage (dq_seat_{CODE}), so resuming from a NEW device
    needs the server to hand it back. Knox auth makes this safe: only this
    game's host (200) — a second Knox user gets 403, anonymous 401. Works for
    any game status (host-private, harmless); the frontend only offers Resume
    on unfinished games.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, code):
        game = get_object_or_404(Game, code=code.upper())
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can recover its seat."}, status=status.HTTP_403_FORBIDDEN)
        seat = game.participants.filter(role=ParticipantRole.HOST).order_by("id").first()
        if seat is None:  # shouldn't happen (created with the game) but stay clean
            return Response({"detail": "This game has no host seat."}, status=status.HTTP_409_CONFLICT)
        return Response(
            {"participant": ParticipantSerializer(seat).data, "participant_token": seat.token}
        )


class GameBoardDetailView(APIView):
    """GET /api/games/<code>/board/ — the host's own board WITH questions and
    answers (Handoff #8 §J3: the lobby preview). Host-only over Knox — the
    §F1 answer-endpoint pattern extended to the whole board. Never any part
    of a snapshot or WS payload, so rule 5 holds: participants and
    unauthenticated clients cannot see questions pre-open or answers
    pre-reveal through this or any other path.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, code):
        game = get_object_or_404(
            Game.objects.prefetch_related("columns__cells__question", "columns__category"),
            code=code.upper(),
        )
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can view its board."}, status=status.HTTP_403_FORBIDDEN)
        return Response(BoardDetailSerializer(game).data)


class CellReplaceView(APIView):
    """POST /api/games/<code>/cells/<cell_id>/replace/ — lobby-only redraw of
    one cell (Handoff #8 §J3). Host-only; same category, closest difficulty
    to the outgoing question, §J2 preference order, excluding questions
    already on the board. 409 once the game has started (or when the
    category has nothing left to swap in). Returns the updated cell in the
    board-detail shape so the preview list can patch in place.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, code, cell_id):
        game = get_object_or_404(Game, code=code.upper())
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can replace questions."}, status=status.HTTP_403_FORBIDDEN)
        try:
            cell = replace_cell_question(code=game.code, cell_id=cell_id, host=request.user)
        except ActionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(BoardDetailCellSerializer(cell).data)


class GameHistoryView(generics.ListAPIView):
    """GET /api/games/history/ — the requesting user's hosted games,
    newest-first, DRF-paginated (Handoff #6 §G2). Registered BEFORE the
    games/<code>/ snapshot route so the static path can't be swallowed by the
    code lookup ("history" would otherwise parse as a code).

    §I (Handoff #8): unfinished games (status lobby/active) were ALWAYS in
    this list — the queryset never filtered by status and each row carries
    `status` — so Resume needed no backend change here, just the pinned test
    below and the host-seat recovery endpoint above."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = GameHistorySerializer

    def get_queryset(self):
        return (
            Game.objects.filter(host=self.request.user)
            .annotate(player_count=Count("participants", filter=Q(participants__role=ParticipantRole.PLAYER)))
            .prefetch_related("winners")
            .order_by("-created_at", "-id")
        )


class GameReportView(APIView):
    """GET /api/games/<code>/report/ — full post-game detail, host-only.

    Chosen non-finished behavior (Handoff #6 §G2, pinned by tests): 409 until
    the game is finished. Reports therefore only ever serialize finished
    games, so including every question's answer can't leak an unrevealed
    answer from a live game (rule 5).
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, code):
        game = get_object_or_404(
            Game.objects.prefetch_related(
                "columns__cells__question", "columns__cells__answered_by", "columns__category", "participants", "winners"
            ),
            code=code.upper(),
        )
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can view its report."}, status=status.HTTP_403_FORBIDDEN)
        if game.status != GameStatus.FINISHED:
            return Response(
                {"detail": "The report is available once the game is finished."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(GameReportSerializer(game, context={"request": request}).data)
