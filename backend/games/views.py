from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.quotas import base_limits_for, quota_denial, tournaments_used
from billing.access import (
    PACK_INACTIVE,
    PLAN_REQUIRED,
    attached_live_pass_tournaments,
    can_host_own_custom,
    can_paid_write,
    unconsumed_active_passes,
)
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from trivia.models import Category as TriviaCategory

from .emails import send_board_email
from .models import Game, GameStatus, Participant, ParticipantRole, Tournament, TournamentAdvancer
from .serializers import (
    BoardDetailCellSerializer,
    BoardDetailColumnSerializer,
    BoardDetailSerializer,
    ColumnCategoryReplaceSerializer,
    CreateGameSerializer,
    GameHistorySerializer,
    GameReportSerializer,
    GameStateSerializer,
    JoinGameSerializer,
    ParticipantSerializer,
    TournamentAdvancerSerializer,
    TournamentDetailSerializer,
    TournamentSerializer,
)
from .services import (
    ActionError,
    StructuredActionError,
    advance_round,
    create_game,
    replace_cell_question,
    replace_column_category,
)

# §I (Handoff #13): the pinned tournament_finished payload — the SAME exact
# shape from both places it can fire (attach at game creation, advance).
TOURNAMENT_FINISHED = {
    "detail": "This tournament is finished — reopen isn't a thing; start a new one.",
    "code": "tournament_finished",
}


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
        # §I (#13): resolve the optional tournament attach. Ownership +
        # liveness ride an owner-scoped lookup (a foreign or soft-deleted id
        # is indistinguishable from a missing one — 404, no existence leak);
        # a finished tournament rejects with the pinned tournament_finished
        # 409. The both-or-neither pairing is the SERVICE's check (rule 4 —
        # the suite calls it directly).
        tournament = None
        tournament_id = serializer.validated_data.get("tournament")
        if tournament_id is not None:
            tournament = Tournament.objects.filter(
                pk=tournament_id, owner=request.user, deleted_at__isnull=True
            ).first()
            if tournament is None:
                return Response({"detail": "No such tournament."}, status=status.HTTP_404_NOT_FOUND)
            if tournament.finished_at is not None:
                return Response(TOURNAMENT_FINISHED, status=status.HTTP_409_CONFLICT)
            # §F8 slice (#18): a PASS-funded tournament carries its pass's
            # limits into every game attach — the pass must be inside its
            # prep window, the game count under game_limit (house-shaped
            # quota_tournament_games), and rounds capped at 3 (C-2 default:
            # LIMITS, not the 3-2-1 shape — a 4-game pub night still works).
            entitlement = getattr(tournament, "entitlement", None)
            if entitlement is not None:
                if not entitlement.is_active:
                    return Response(dict(PACK_INACTIVE), status=status.HTTP_403_FORBIDDEN)
                round_number = serializer.validated_data.get("round_number")
                if round_number is not None and round_number > 3:
                    return Response(
                        {"round_number": ["Tournament passes cover rounds 1–3."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if entitlement.game_limit is not None:
                    used = tournament.games.count()
                    if used >= entitlement.game_limit:
                        return Response(
                            {
                                "detail": (
                                    f"This tournament pass covers {entitlement.game_limit} games "
                                    f"({used} created)."
                                ),
                                "code": "quota_tournament_games",
                                "used": used,
                                "limit": entitlement.game_limit,
                            },
                            status=status.HTTP_403_FORBIDDEN,
                        )
        # §F5 (#18): hand-picking is a PAID convenience — any active
        # entitlement or a staff-granted creator plan (C-3 default). The UI
        # hides the picker otherwise; this is the real gate.
        hand_picked = serializer.validated_data.get("hand_picked") or None
        if hand_picked and not can_paid_write(request.user):
            return Response(
                {
                    "detail": "Hand-picking questions comes with any paid plan or pack.",
                    "code": "hand_pick_locked",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        # §F4/§F7 (#18): hosting gates on the picked categories. One query;
        # HTTP-shaped here (the tournament-resolve precedent), exact shapes
        # pinned: a host's own BOUND category needs its pack ACTIVE
        # (pack_inactive names reactivation); a host's own UNBOUND custom
        # category needs authoring rights (plan_required). Free-library
        # boards stay free for everyone — including lapsed buyers.
        picked_categories = TriviaCategory.objects.filter(
            id__in=serializer.validated_data["categories"], deleted_at__isnull=True
        ).select_related("entitlement__source_subscription")
        own_unbound = False
        for category in picked_categories:
            if category.entitlement_id is not None and category.owner_id == request.user.id:
                if not category.entitlement.is_active:
                    return Response(dict(PACK_INACTIVE), status=status.HTTP_403_FORBIDDEN)
            elif category.owner_id == request.user.id:
                own_unbound = True
        if own_unbound and not can_host_own_custom(request.user):
            return Response(dict(PLAN_REQUIRED), status=status.HTTP_403_FORBIDDEN)
        game = create_game(
            host=request.user,
            mode=serializer.validated_data["mode"],
            category_ids=serializer.validated_data["categories"],
            questions_per_category=serializer.validated_data["questions_per_category"],
            # §H (#13): the host's per-game sound choice (serializer default 1).
            buzz_sound=serializer.validated_data["buzz_sound"],
            thunder=serializer.validated_data["thunder"],  # #21.1: ⚡ opt-out
            tournament=tournament,
            round_number=serializer.validated_data.get("round_number"),
            hand_picked=hand_picked,
        )
        host_participant = Participant.objects.create(
            game=game,
            name=request.user.display_name or "Host",
            role=ParticipantRole.HOST,
        )
        # §F1e (#18): the board backup email — host-only content is fine (the
        # host already sees answers via the console); failure to send must
        # never fail the create (send_board_email warns and continues).
        send_board_email(game)
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
        # select_related("host"): the join response serializes a snapshot,
        # and §H's brand field reads game.host.
        # + "tournament" (§I #13): the join response serializes a snapshot too.
        game = get_object_or_404(Game.objects.select_related("host", "tournament"), code=code.upper())
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
            # §F (#11): a removed seat's token must NOT reclaim it — filter
            # removed and fall through to a normal fresh-seat join attempt
            # (their old name is reusable thanks to the partial constraint).
            participant = Participant.objects.filter(game=game, token=token, removed_at__isnull=True).first()
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
                # §F (#11): removed seats don't count — kicking frees a seat.
                # The buzzer_sound round-robin below rides the same count.
                player_count = locked_game.participants.filter(
                    role=ParticipantRole.PLAYER, removed_at__isnull=True
                ).count()
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
    """GET /api/games/<code>/ — full board snapshot (also pushed over WS).

    §F3b (Handoff #20): `?seat=<participant_token>` — optional, and the ONE
    way the additive `my_advancement` key stops being null: the token must
    resolve to an ACTIVE participant of THIS game, and only that seat's own
    advancement is disclosed (rank + target code/status + claimed — never
    anyone's ids or tokens, §B rule 5). Everyone else, including every WS
    broadcast (which is group-shared by construction), gets null. The
    buzzer's finish screen polls this — advancement happens minutes after
    the last snapshot broadcast, so a passive payload could never carry it.
    """

    permission_classes = (permissions.AllowAny,)

    def get(self, request, code):
        game = get_object_or_404(
            Game.objects.prefetch_related("columns__cells", "participants", "columns__category")
            # "host" feeds the brand; "tournament" feeds §I's identity
            # block (no per-snapshot queries for either).
            # current_cell__thunder_chugger feeds §F2's (#21) chug block.
            .select_related(
                "current_cell__question", "current_cell__thunder_chugger",
                "judged_participant", "host", "tournament",
            ),
            code=code.upper(),
        )
        context = {"request": request}
        seat_token = request.query_params.get("seat")
        if seat_token:
            context["seat_participant"] = next(
                (
                    p
                    for p in game.participants.all()
                    if p.token == seat_token and p.removed_at is None
                ),
                None,
            )
        return Response(GameStateSerializer(game, context=context).data)


class GameClaimView(APIView):
    """POST /api/games/<target-code>/claim/  {participant_token}

    §F3a (Handoff #20): a qualifier seats THEMSELVES into the next round —
    no account, no re-typing. The ROUND-1 participant token is the proof
    (the same no-account auth every game surface uses; #19's C-2 keeps it
    on the phone through logout). Server verifies, in the handoff's order:
    token → active participant P (401 otherwise); P's game FINISHED; an
    advancer row links source_participant=P to THIS game; the target is in
    LOBBY (C-5 — manual join-by-code keeps working the whole time and after);
    the claim is unspent (the §F1c partial unique is the DB truth behind the
    check); the advancer's NAME is free among active seats (C-8: structured
    error naming the conflict, no silent suffixing — the host resolves).
    Chosen ruling (flagged): the MAX_PLAYERS cap applies to claims too, with
    the exact pinned game_full shape — a 7th seat is a 7th seat however it
    arrives; the host kicks a walk-up to free one (the C-8 pattern).
    Then: create the seat (role=player, name=advancer.name, claimed_from=P,
    the join round-robin buzzer_sound) and answer EXACTLY like the join
    endpoint — the buzzer stores it via the normal saveSeat path.
    """

    permission_classes = (permissions.AllowAny,)

    def post(self, request, code):
        target = get_object_or_404(
            Game.objects.select_related("host", "tournament"), code=code.upper()
        )
        token = request.data.get("participant_token") or ""
        source = (
            Participant.objects.select_related("game")
            .filter(token=token, removed_at__isnull=True)
            .first()
        )
        if source is None:
            return Response(
                {"detail": "That buzzer token isn't valid anymore."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if source.game.status != GameStatus.FINISHED:
            return Response(
                {
                    "detail": "Your game hasn't finished yet — seats open once it has.",
                    "code": "claim_source_unfinished",
                },
                status=status.HTTP_409_CONFLICT,
            )
        advancer = (
            TournamentAdvancer.objects.filter(source_participant=source, target_game=target)
            .order_by("-id")
            .first()
        )
        if advancer is None:
            # One honest code for both "didn't qualify" and "wrong target" —
            # either way, no advancer row routes THIS seat into THIS game.
            return Response(
                {
                    "detail": "This seat didn't advance into that game — check with your host.",
                    "code": "claim_not_qualified",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if target.status != GameStatus.LOBBY:
            return Response(
                {
                    "detail": "That game has already started — join it by code if your host says so.",
                    "code": "claim_target_started",
                },
                status=status.HTTP_409_CONFLICT,
            )
        if Participant.objects.filter(game=target, claimed_from=source).exists():
            return Response(
                {
                    "detail": "This seat was already claimed for that game.",
                    "code": "claim_already_claimed",
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            with transaction.atomic():
                locked = Game.objects.select_for_update().get(pk=target.pk)
                if locked.status != GameStatus.LOBBY:
                    return Response(
                        {
                            "detail": "That game has already started — join it by code if your host says so.",
                            "code": "claim_target_started",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                active = list(
                    locked.participants.filter(
                        role=ParticipantRole.PLAYER, removed_at__isnull=True
                    )
                )
                if any(p.name == advancer.name for p in active):
                    # C-8: fail LOUD and structured; two "TEAM A (2)"s on a
                    # bracket is worse than a 10-second host fix.
                    return Response(
                        {
                            "detail": (
                                f"“{advancer.name}” is already seated in that game — "
                                "ask your host to sort the seat, then tap again."
                            ),
                            "code": "claim_name_taken",
                            "name": advancer.name,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                if len(active) >= settings.MAX_PLAYERS_PER_GAME:
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
                    game=locked,
                    name=advancer.name,
                    claimed_from=source,
                    buzzer_sound=(len(active) % 4) + 1,
                )
        except IntegrityError:
            # OUTSIDE the atomic block (house rule). Either partial unique
            # (name, or the §F1c claim ledger) racing another request lands
            # here; the room's next tap tells the truth.
            return Response(
                {
                    "detail": "That seat was just taken — ask your host, then try again.",
                    "code": "claim_conflict",
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {
                "participant": ParticipantSerializer(participant).data,
                "participant_token": participant.token,
                "game": GameStateSerializer(target, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )


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


class ColumnCategoryReplaceView(APIView):
    """POST /api/games/<code>/columns/<column_id>/replace/ {category_id} —
    lobby-only swap of one column's CATEGORY (Handoff #16 §F): tear the
    column's cells down and rebuild them from the incoming category via the
    same draw + value scaling creation uses. Host-only over Knox; the
    service's lobby/duplicate/deleted/shortage rejections map to structured
    409s (the cell-replace pattern, directly above). Returns the updated
    column in the board-detail shape so the lobby preview patches ONE
    column in place. The body is category-ID based on purpose — board
    building and board edits both stay theme-unaware (§G #10).
    """

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, code, column_id):
        game = get_object_or_404(Game, code=code.upper())
        if game.host_id != request.user.id:
            return Response({"detail": "Only this game's host can swap categories."}, status=status.HTTP_403_FORBIDDEN)
        serializer = ColumnCategoryReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            column = replace_column_category(
                code=game.code,
                column_id=column_id,
                new_category_id=serializer.validated_data["category_id"],
                host=request.user,
            )
        except ActionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(BoardDetailColumnSerializer(column).data)


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
            # §F (#11): active players only — a kicked seat is not a team.
            .annotate(
                player_count=Count(
                    "participants",
                    filter=Q(participants__role=ParticipantRole.PLAYER, participants__removed_at__isnull=True),
                )
            )
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


# --- §I (Handoff #13): tournament mode v1 -----------------------------------
# CRUD-lite, ALL host-private over Knox (participants hold participant
# tokens, not Knox). Every lookup is OWNER-scoped, so a foreign, unknown or
# soft-deleted id reads as a plain 404 — no existence leak, and "other
# people's tournament" can never be touched (rule 4). Nothing in any payload
# here carries question content (rule 5 — pinned the grep way in tests).


def _own_live_tournament(request, pk):
    """The one lookup: mine + not soft-deleted, else 404."""
    return get_object_or_404(
        Tournament.objects.filter(owner=request.user, deleted_at__isnull=True), pk=pk
    )


class TournamentListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/tournaments/ — own live tournaments, newest first;
    create is plan-gated through the quota choke point (free's limit is 0 →
    the structured quota_tournaments 403, the categories pattern — and a
    staff limit_overrides grant works)."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = TournamentSerializer

    def get_queryset(self):
        return (
            Tournament.objects.filter(owner=self.request.user, deleted_at__isnull=True)
            .order_by("-created_at", "-id")
        )

    def create(self, request, *args, **kwargs):
        if denial := quota_denial(request.user, "tournaments"):
            # §F4c (#21), C-8: for a pass holder the generic "plan's limit"
            # line is both confusing (they bought a pass, not a plan) and
            # the old copy's road to the delete trap. When an ACTIVE pass is
            # attached to a LIVE tournament, the detail NAMES it instead —
            # the frontend links it via the usage summary's `tournament`
            # block. STRING only; the pinned quota-403 family shape
            # {detail, code, used, limit} is untouched.
            running = attached_live_pass_tournaments(request.user)
            if running:
                denial = {
                    **denial,
                    "detail": (
                        f"Your Tournament Pass is running “{running[0].name}” — "
                        "open it from your tournaments list. A new tournament needs a new pass."
                    ),
                }
            return Response(denial, status=status.HTTP_403_FORBIDDEN)
        # §F8 slice (#18): the union in limits_for counts ACTIVE passes into
        # the tournaments allowance; CONSUMPTION happens here. If the plain
        # lane (plan + overrides, no union) covers this create on its own,
        # no pass is touched; otherwise the OLDEST unconsumed active pass is
        # bound to the new tournament (Entitlement.tournament OneToOne = the
        # "one pass, one tournament" rule as a DB constraint). A user whose
        # only active pass is already spent gets the house-shaped 403 even
        # though the union math said yes — the pass, not the count, is the
        # real allowance (flagged ruling).
        plain_limit = base_limits_for(request.user).get("tournaments")
        plain_ok = plain_limit is None or tournaments_used(request.user) + 1 <= plain_limit
        consume = None
        if not plain_ok:
            passes = sorted(unconsumed_active_passes(request.user), key=lambda e: (e.created_at, e.id))
            if not passes:
                used = tournaments_used(request.user)
                # §F4c (#21), C-8: the copy stops hinting at deletion (the
                # trap this handoff removes) and NAMES the running
                # tournament instead — the frontend links it via the usage
                # summary's additive `tournament` block. Shape unchanged:
                # the pinned quota-403 family {detail, code, used, limit}.
                running = attached_live_pass_tournaments(request.user)
                if running:
                    detail = (
                        f"Your Tournament Pass is running “{running[0].name}” — "
                        "open it from your tournaments list. A new tournament needs a new pass."
                    )
                else:
                    detail = (
                        "Your tournament pass is already attached to a tournament — "
                        "a new tournament needs a new pass."
                    )
                return Response(
                    {
                        "detail": detail,
                        "code": "quota_tournaments",
                        "used": used,
                        "limit": plain_limit or 0,
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            consume = passes[0]
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # IntegrityError caught OUTSIDE the atomic block (house rule):
            # the partial unique (owner, name where live) is the gate for
            # duplicate live names — DRF can't auto-validate a conditional
            # constraint, so the DB is the truth, exactly like join names.
            with transaction.atomic():
                tournament = serializer.save(owner=request.user)
                if consume is not None:
                    consume.tournament = tournament
                    consume.save(update_fields=["tournament"])
        except IntegrityError:
            return Response(
                {"name": ["You already have a live tournament with that name."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(TournamentSerializer(tournament).data, status=status.HTTP_201_CREATED)


class TournamentDetailView(APIView):
    """GET /api/tournaments/<pk>/ — the control-room payload (games +
    standings + advancers).

    DELETE — §F4b (#21), C-8: REMOVED for owners (a plain structured 409;
    deliberately removed, not deferred — §M). Deleting looked like the way
    to free a stuck pass, silently orphaned real history, and the pass bug
    it "solved" is fixed at the source (billing.access, live-only counting)
    — so the trap goes. STAFF keep the pre-#21 soft delete as the support
    escape hatch (the house liveness flag: the name frees via the partial
    constraint, attached games keep their history), and the Django admin is
    untouched."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, pk):
        tournament = get_object_or_404(
            Tournament.objects.filter(owner=request.user, deleted_at__isnull=True).prefetch_related(
                Prefetch(
                    "games",
                    queryset=Game.objects.order_by("round_number", "created_at", "id").prefetch_related(
                        "participants"
                    ),
                ),
                # §F2 (#20): advancer rows now render source AND target codes —
                # select both here so the serializer stays query-free.
                Prefetch(
                    "advancers",
                    queryset=TournamentAdvancer.objects.select_related("source_game", "target_game"),
                ),
            ),
            pk=pk,
        )
        return Response(TournamentDetailSerializer(tournament).data)

    def delete(self, request, pk):
        tournament = _own_live_tournament(request, pk)
        if not request.user.is_staff:
            # Exactly {detail, code} — the documented plain structured shape.
            return Response(
                {
                    "detail": (
                        "Tournaments can't be deleted — your pass stays attached to "
                        "its tournament. Email us if something's wrong."
                    ),
                    "code": "tournament_delete_disabled",
                },
                status=status.HTTP_409_CONFLICT,
            )
        tournament.deleted_at = timezone.now()
        tournament.save(update_fields=["deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TournamentFinishView(APIView):
    """POST /api/tournaments/<pk>/finish/ — seal it. IDEMPOTENT like
    finalize_game: a second finish keeps the first finished_at (history is
    never rewritten) and still 200s with the current row."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        tournament = _own_live_tournament(request, pk)
        if tournament.finished_at is None:
            tournament.finished_at = timezone.now()
            tournament.save(update_fields=["finished_at"])
        return Response(TournamentSerializer(tournament).data)


class TournamentAdvancerTargetView(APIView):
    """POST /api/tournaments/<pk>/advancers/<advancer_id>/target/
    {game: "<CODE>" | null} — §F2 (#20): the host routes ONE qualifier into
    one next-round game (C-4's multi-game lane; single-game rounds auto-set
    and never need this). Owner-scoped like every tournament surface; the
    game must belong to THIS tournament and sit in round N+1 (400 otherwise);
    null clears back to "ask your host". A finished tournament rejects with
    the pinned tournament_finished 409, exactly like advance."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk, advancer_id):
        tournament = _own_live_tournament(request, pk)
        if tournament.finished_at is not None:
            return Response(TOURNAMENT_FINISHED, status=status.HTTP_409_CONFLICT)
        advancer = get_object_or_404(
            TournamentAdvancer.objects.filter(tournament=tournament), pk=advancer_id
        )
        raw = request.data.get("game")
        if raw is None or raw == "":
            advancer.target_game = None
        else:
            game = tournament.games.filter(code=str(raw).upper()).first()
            if game is None:
                return Response(
                    {"game": ["No such game in this tournament."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if game.round_number != advancer.round_number + 1:
                return Response(
                    {"game": [f"Pick a round {advancer.round_number + 1} game."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            advancer.target_game = game
        advancer.save(update_fields=["target_game"])
        return Response(TournamentAdvancerSerializer(advancer).data)


class TournamentAdvanceView(APIView):
    """POST /api/tournaments/<pk>/rounds/<round_number>/advance/
    {per_game: 1|2} — compute + persist who goes through (see
    services.advance_round for the chosen re-runnable semantics). Host-only;
    a finished tournament rejects with the same pinned tournament_finished
    409 the attach path uses."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk, round_number):
        tournament = _own_live_tournament(request, pk)
        if tournament.finished_at is not None:
            return Response(TOURNAMENT_FINISHED, status=status.HTTP_409_CONFLICT)
        per_game = request.data.get("per_game", 1)
        # Strict ints only (bools are ints in Python — rejected explicitly,
        # the validate_overrides convention).
        if isinstance(per_game, bool) or per_game not in (1, 2):
            return Response(
                {"per_game": ["Advance the top 1 or the top 2 per game."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            rows = advance_round(tournament=tournament, round_number=round_number, per_game=per_game)
        except StructuredActionError as exc:
            # tournament_round_empty / tournament_round_incomplete — exact
            # documented payloads (C4), same shapes the WS relays use.
            return Response(exc.payload, status=status.HTTP_409_CONFLICT)
        except IntegrityError:
            # OUTSIDE the atomic block (house rule). The tournament row lock
            # makes this near-impossible, but the foot-gun list says catch it.
            return Response(
                {"detail": "Advancement raced another request — try again."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {
                "round_number": round_number,
                "per_game": per_game,
                "advancers": TournamentAdvancerSerializer(rows, many=True).data,
            }
        )
