from rest_framework import serializers

from .models import BoardCell, BoardColumn, Buzz, CellState, Game, GameStatus, Participant, Tournament, TournamentAdvancer


class ParticipantSerializer(serializers.ModelSerializer):
    # buzzer_sound (§I): 1-4, present in the snapshot and the join response so
    # clients read their sound from server state, never local guessing.
    # buzz_checked (§F3 #21, ADDITIVE): the lobby test-smash landed for this
    # seat — the host lobby and the TV lobby render the ✓ from it (C-7).
    # Derived, never the raw timestamp (nobody needs the when).
    buzz_checked = serializers.SerializerMethodField()

    class Meta:
        model = Participant
        fields = (
            "id", "name", "role", "score", "drinks_taken", "drinks_given", "connected",
            "buzzer_sound", "buzz_checked",
        )

    def get_buzz_checked(self, participant):
        return participant.buzz_checked_at is not None


class BuzzSerializer(serializers.ModelSerializer):
    participant_id = serializers.IntegerField(source="participant.id", read_only=True)
    name = serializers.CharField(source="participant.name", read_only=True)

    class Meta:
        model = Buzz
        fields = ("participant_id", "name", "created_at")


class CellSerializer(serializers.ModelSerializer):
    """Cell as visible to players — no question text or answer until opened."""

    class Meta:
        model = BoardCell
        fields = ("id", "row", "value", "state", "answered_by", "answered_correctly")


class OpenCellSerializer(serializers.ModelSerializer):
    """The currently open cell, with question content (never the answer).

    Handoff #8: also carries §F's `last_judgment` and §G's drink state
    (`drinks_assigned` + `drink_assignment`) — snapshot fields, so WS boards,
    polling boards and phones all render them with zero hook changes (§B4),
    and every button's enabled/disabled state derives from here (rule 1).
    Deliberately still NO question id: an authenticated user could resolve a
    question id to its answer via /api/questions/, so exposing it mid-game
    would be a rule-5 side door. The host gets ids through the host-private
    answer/board endpoints instead."""

    question_text = serializers.CharField(source="question.question_text", read_only=True)
    media_type = serializers.CharField(source="question.media_type", read_only=True)
    image = serializers.FileField(source="question.image", read_only=True)
    audio = serializers.FileField(source="question.audio", read_only=True)
    video = serializers.FileField(source="question.video", read_only=True)
    buzzes = BuzzSerializer(many=True, read_only=True)
    last_judgment = serializers.SerializerMethodField()
    drink_assignment = serializers.SerializerMethodField()

    class Meta:
        model = BoardCell
        fields = (
            "id", "row", "value", "state", "question_text", "media_type", "image", "audio", "video",
            "buzzes", "last_judgment", "drinks_assigned", "drink_assignment",
        )

    def _game(self, cell):
        # Nested use (the normal case): the root serializer's instance IS the
        # game — no extra query. Standalone fallback: the cell's own FK.
        root_instance = getattr(self.root, "instance", None)
        return root_instance if isinstance(root_instance, Game) else cell.game

    def get_last_judgment(self, cell):
        """§F: {"participant_id", "name", "correct"} while a verdict is
        current, else null. Storage is Game.judged_* (cleared on open/close/
        reset/explicit reopen/next buzz), so it is inherently scoped to the
        cell being shown."""
        game = self._game(cell)
        if game.judged_participant_id is None or game.judged_correct is None:
            return None
        participant = game.judged_participant
        return {"participant_id": participant.id, "name": participant.name, "correct": game.judged_correct}

    def get_drink_assignment(self, cell):
        """§G attribution for the board line ("TEAM A sends a drink to THE
        HOST 🍺"): the cell's assignment, or null. Oldest first so legacy
        games that pre-date the once-per-cell rule show their first one."""
        assignment = (
            cell.drink_assignments.select_related("from_participant", "to_participant")
            .order_by("created_at", "id")
            .first()
        )
        if assignment is None:
            return None
        return {
            "from_participant_id": assignment.from_participant_id,
            "from_name": assignment.from_participant.name,
            "to_participant_id": assignment.to_participant_id,
            "to_name": assignment.to_participant.name,
            "amount": assignment.amount,
        }

    def to_representation(self, cell):
        """§F2 (#21): Thunder shaping, on the OPEN cell only.

        `thunder: true` rides the payload ONLY when the cell is Thunder —
        never a `thunder: false` on normal cells, so the §B no-spoiler grep
        ("no 'thunder' anywhere while a Thunder cell sits unopened") holds
        by construction. During stage "fanfare" the question CONTENT is
        withheld (keys stay, values null — shape-stable): the board and the
        buzzer show the ⚡ splash, and the text enters the payload only at
        the host's reveal. Rule 5 untouched: the ANSWER path is still
        exclusively `revealed_answer`.
        """
        data = super().to_representation(cell)
        if cell.is_thunder:
            data["thunder"] = True
            if not cell.thunder_revealed:
                for key in ("question_text", "media_type", "image", "audio", "video"):
                    data[key] = None
        return data


def chug_state(game) -> dict | None:
    """§F2 (#21): the snapshot's top-level `chug` block — THE documented
    shape, pinned by an exact-set test (ThunderFlowTests):

        null                            — no Thunder cell is open
        {"stage":        "fanfare" | "answering" | "pick" | "ready" | "running",
         "wager":        int | null,    — the shouted seconds (C-3: 3–30)
         "chugger_name": str | null,    — WHO drinks, as a NAME (never an id
                                          or token on public surfaces — §B)
         "started_at":   iso8601 | null,— the server clock anchor (C-5)
         "seconds":      int | null}    — the countdown length; equals wager
                                          (kept explicit so countdown code
                                          never reaches into judging fields)

    All five keys ALWAYS present when the block exists. Stage derives
    entirely from persisted cell state (see services: the §F2 stage
    machine), so WS broadcasts, REST polls and reloads can never disagree.
    """
    cell = game.current_cell
    if cell is None or not cell.is_thunder:
        return None
    if not cell.thunder_revealed:
        stage = "fanfare"
    elif cell.answered_correctly is None:
        stage = "answering"
    elif cell.thunder_chugger_id is None:
        stage = "pick"
    elif cell.thunder_started_at is None:
        stage = "ready"
    else:
        stage = "running"
    chugger = cell.thunder_chugger
    return {
        "stage": stage,
        "wager": cell.thunder_wager,
        "chugger_name": chugger.name if chugger is not None else None,
        "started_at": cell.thunder_started_at.isoformat() if cell.thunder_started_at else None,
        "seconds": cell.thunder_wager,
    }


class ColumnSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_photo = serializers.FileField(source="category.photo", read_only=True)
    cells = CellSerializer(many=True, read_only=True)

    class Meta:
        model = BoardColumn
        fields = ("id", "position", "category_name", "category_photo", "cells")


class GameStateSerializer(serializers.ModelSerializer):
    """Full snapshot: sent over REST and on every WebSocket (re)connect,
    which is what makes a page reload lossless."""

    columns = ColumnSerializer(many=True, read_only=True)
    # §F (Handoff #11): REMOVED participants are excluded from `participants`
    # entirely — clients are dumb renderers, so a kicked seat simply stops
    # existing on every screen (ScoreStrip, FinalStandings, lobby lists,
    # drink pickers all clean themselves with zero component changes).
    participants = serializers.SerializerMethodField()
    # §F/§G (Handoff #11): removed players who still hold a cell attribution
    # (BoardCell.answered_by) — just enough for §G's answered-cell names to
    # resolve an id the `participants` list no longer carries. Derived from
    # the SAME prefetched participants + columns__cells every snapshot caller
    # already loads (the cells_remaining pattern) — no fresh queries.
    former_players = serializers.SerializerMethodField()
    current_cell = OpenCellSerializer(read_only=True)
    # Handoff #6 §F2: null at all times EXCEPT between the host's reveal and
    # close_cell — this is the one sanctioned way an answer enters a snapshot
    # (rule 5), and it's assembled here, not in OpenCellSerializer, which keeps
    # excluding `answer` unconditionally. REST-polling boards render the reveal
    # from this field; WS boards keep the `answer_reveal` event too.
    revealed_answer = serializers.SerializerMethodField()
    # §G: the player cap, surfaced in the snapshot so lobby copy ("N/6 teams")
    # stays a dumb render of server state (rule 1) instead of a hardcoded 6.
    max_players = serializers.SerializerMethodField()
    # §H (Handoff #11): venue branding — {"name", "logo"} from the HOST's
    # profile, or null. Served while the host holds an active branding lane
    # (manual creator plan OR an active venue-kind entitlement — §F3(d),
    # Handoff #19; a lapse of both turns branding off without destroying
    # the upload) and something is actually set. A snapshot field, so the TV
    # lobby, the in-play header and the buzzer join line all get it over
    # BOTH transports for free (C2). Every snapshot caller select_related's
    # "host" (checked for N+1 like cells_remaining was).
    brand = serializers.SerializerMethodField()
    # §I (Handoff #13): tournament identity — {"name", "location",
    # "round_number"} | null. `location` is resolved SERVER-side (clients
    # are dumb renderers): the tournament's own location, else the host's
    # brand_name, else their display name, else null (the frontend then just
    # hides the hosted-by line). Every snapshot caller select_related's
    # "tournament" (the same no-N+1 rule "host" follows). Deliberately NOT
    # plan-gated on serve: creation was the gate — hiding "Round 2" on a
    # mid-tournament plan lapse would break a live event (CHANGES.md).
    tournament = serializers.SerializerMethodField()
    # §F3b (Handoff #20): tournament self-seeding, YOUR OWN seat only —
    # null on every snapshot EXCEPT the REST GET carrying a valid
    # `?seat=<participant_token>` for a FINISHED tournament game (the view
    # resolves the token to `seat_participant` in context; WS broadcasts are
    # group-shared, so they can never carry it by construction). Shape:
    # null | {"rank", "target": {"code","status","round_number"}|null,
    # "claimed"} — the target's join code is projector-public already; no
    # participant ids or tokens, ever (§B rule 5). Null is deliberately
    # ambiguous between "host hasn't advanced yet" and "didn't qualify" —
    # the host announces results, not this payload.
    my_advancement = serializers.SerializerMethodField()
    # §F2 (#21): the THUNDER FUCKED live block — null except during a
    # Thunder cell's life. Shape + stage machine documented on chug_state
    # above (the module-level function so the suite can call it directly).
    # ADDITIVE per §B; a snapshot field, so WS boards, polling boards and
    # phones all render the same stages with zero transport special-casing.
    chug = serializers.SerializerMethodField()
    # §H1 (Handoff #10): cells not yet ANSWERED — derived here, never stored,
    # so the snapshot stays the source of truth and BOTH transports (WS and
    # the polling board) get it for free (C2). An OPEN cell still counts as
    # remaining, so the count hits 0 only after the LAST close_cell — exactly
    # when the host is back at the fully played grid and the finish prompt
    # should appear. questions_per_category × columns at lobby; decrements
    # per close; a reveal/judgment alone never changes it.
    cells_remaining = serializers.SerializerMethodField()

    class Meta:
        model = Game
        fields = (
            # §H (#13): top-level `buzz_sound` — the host's per-game choice;
            # buzzers AND the board play this one (the cells_remaining
            # "snapshot field, both transports" template, as a plain model
            # field). The per-participant `buzzer_sound` inside
            # `participants` is now VESTIGIAL but stays in the payload for
            # one session (C12: the suite and the smoke assert it; removing
            # a public field is #14 territory after clients migrate — §M).
            "code", "mode", "status", "questions_per_category", "buzz_sound", "max_players",
            "cells_remaining", "buzzer_open", "current_cell", "chug", "revealed_answer", "columns",
            "participants", "former_players", "brand", "tournament", "my_advancement", "created_at",
        )

    def get_chug(self, game):
        return chug_state(game)

    def get_participants(self, game):
        # Filtered in Python over the prefetched list (never .filter(), which
        # would re-query and defeat every caller's prefetch_related).
        active = [p for p in game.participants.all() if p.removed_at is None]
        return ParticipantSerializer(active, many=True).data

    def get_former_players(self, game):
        # Ids holding an attribution, from the prefetched columns__cells —
        # same pass `columns` serializes (the cells_remaining pattern).
        answered_ids = {
            cell.answered_by_id
            for column in game.columns.all()
            for cell in column.cells.all()
            if cell.answered_by_id is not None
        }
        return [
            {"id": p.id, "name": p.name}
            for p in game.participants.all()
            if p.removed_at is not None and p.id in answered_ids
        ]

    def get_brand(self, game):
        host = game.host
        name = host.brand_name or None
        logo = host.brand_logo or None
        if name is None and logo is None:
            return None  # nothing set — the common case costs zero extra queries
        # §F3(d) (Handoff #19): plan alone is wrong for buyers (§A.1) — the
        # Venue promise is "your branding on every screen", and Venue buyers
        # stay plan:"free". Serve for a manual creator OR an ACTIVE
        # venue-kind entitlement; a lapse of BOTH lanes turns branding off
        # without destroying the upload, exactly as before. The entitlement
        # lookup only runs for non-creator hosts who actually SET branding.
        if not host.is_creator:
            from billing.access import venue_active

            if not venue_active(host):
                return None
        url = None
        if logo:
            url = logo.url
            request = self.context.get("request")
            if request is not None:  # WS snapshots have no request; root-relative is fine (C2)
                url = request.build_absolute_uri(url)
        return {"name": name, "logo": url}

    def get_tournament(self, game):
        t = game.tournament
        if t is None:
            return None
        host = game.host  # select_related on every snapshot caller already
        location = t.location or host.brand_name or host.display_name or None
        # §F4b (Handoff #17): pinned-shape AMENDMENT, additive — the block
        # gains `id` so the host console can link back to its bracket
        # (/tournaments/<id>). Safe on player surfaces: the id is inert
        # without the owner's Knox token (the detail endpoint is
        # owner-scoped 404 — pinned in SnapshotTournamentTests), and rule 5
        # (no question content on tournament surfaces) is untouched. Every
        # exact-dict assertion moved in this same session: games/tests.py
        # (TournamentAttachTests + SnapshotTournamentTests) and
        # backend_smoke_test.py's tournament story.
        return {"id": t.id, "name": t.name, "location": location, "round_number": game.round_number}

    def get_my_advancement(self, game):
        seat = self.context.get("seat_participant")
        if (
            seat is None
            or seat.game_id != game.id
            or game.status != GameStatus.FINISHED
            or game.tournament_id is None
        ):
            return None
        advancer = (
            TournamentAdvancer.objects.select_related("target_game")
            .filter(source_participant=seat, source_game=game)
            .order_by("-id")
            .first()
        )
        if advancer is None:
            return None
        target = advancer.target_game
        claimed = (
            target is not None
            and Participant.objects.filter(game=target, claimed_from=seat).exists()
        )
        return {
            "rank": advancer.rank,
            "target": (
                {"code": target.code, "status": target.status, "round_number": target.round_number}
                if target is not None
                else None
            ),
            "claimed": claimed,
        }

    def get_cells_remaining(self, game):
        # Counted from the SAME prefetched columns/cells `columns` serializes
        # in this pass (every snapshot caller prefetches columns__cells) —
        # no fresh query per snapshot, no N+1.
        return sum(
            1
            for column in game.columns.all()
            for cell in column.cells.all()
            if cell.state != CellState.ANSWERED
        )

    def get_max_players(self, game):
        from django.conf import settings

        return settings.MAX_PLAYERS_PER_GAME

    def get_revealed_answer(self, game):
        if game.answer_revealed and game.current_cell_id:
            return game.current_cell.question.answer
        return None


class CreateGameSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("drinks", "points"), default="drinks")
    categories = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=8)
    questions_per_category = serializers.IntegerField(min_value=1, max_value=10, default=5)
    # §H (#13): the host's per-game sound choice — validated 1–4 (rule 4:
    # the picker's four options are cosmetic; 0, 5 and "x" all 400 here),
    # default 1 when omitted so old clients keep working unchanged.
    buzz_sound = serializers.IntegerField(min_value=1, max_value=4, default=1)
    # #21.1 (owner-directed): the ⚡ opt-out — drinks boards mark Thunder
    # cells by DEFAULT; false skips the marking entirely (a play-without
    # night). Creation-time only, fully materialized in is_thunder columns
    # (no stored flag, no migration); harmless on points boards (which
    # never mark regardless). Strict-bool at the service (rule 4).
    thunder = serializers.BooleanField(required=False, default=True)
    # §I (#13): optional tournament attach — BOTH or NEITHER (the service
    # enforces the pairing; the view resolves ownership/liveness/finished).
    tournament = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    round_number = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    # §F5 (#18): optional hand-picked columns — {"<category_id>": [question
    # ids in board order]} for any SUBSET of the picked categories; columns
    # without an entry keep the automatic draw. Shape here, semantics (gate,
    # ownership, length, dups, archived) in the view + service.
    hand_picked = serializers.DictField(
        child=serializers.ListField(child=serializers.IntegerField(min_value=1)),
        required=False,
        allow_null=True,
        default=None,
    )


# --- §I (Handoff #13): tournament payloads ---------------------------------
# ALL host-private (Knox), and by design carrying NO question content — a
# tournament surface is names + scores + game codes only (rule 5; pinned the
# grep way, like #12's public categories).


class TournamentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tournament
        fields = ("id", "name", "location", "created_at", "finished_at")
        read_only_fields = ("id", "created_at", "finished_at")


class TournamentAdvancerSerializer(serializers.ModelSerializer):
    source_game = serializers.CharField(source="source_game.code", read_only=True)
    # §F2 (#20), additive: `id` so the host's target selector can address a
    # row; `target_game` as a CODE (a projected join code — the one currency
    # tournament payloads already speak; §F1's participant linkage stays
    # internal, §B). Null target = "ask your host" on the qualifier's phone.
    target_game = serializers.SerializerMethodField()

    class Meta:
        model = TournamentAdvancer
        fields = ("id", "round_number", "name", "rank", "source_game", "target_game")

    def get_target_game(self, advancer):
        return advancer.target_game.code if advancer.target_game_id else None


class TournamentGameSerializer(serializers.ModelSerializer):
    """One game card in the tournament control room: code/status/standings —
    deliberately nothing board-shaped, nothing question-shaped (rule 5)."""

    standings = serializers.SerializerMethodField()

    class Meta:
        model = Game
        fields = ("code", "mode", "status", "round_number", "created_at", "finished_at", "standings")

    def get_standings(self, game):
        # Standings exist once the game is FINISHED — the same truths the
        # winners computation reads (services.game_standings docstring).
        if game.status != GameStatus.FINISHED:
            return None
        from .services import game_standings

        return game_standings(game)


class TournamentDetailSerializer(TournamentSerializer):
    """GET /api/tournaments/<id>/ — the control-room payload: games grouped
    client-side by round_number, plus every advancer row. The view prefetches
    games (ordered) + their participants + advancers, so nothing here
    queries."""

    games = TournamentGameSerializer(many=True, read_only=True)
    # §F2 (#20): the control room additionally needs claim STATE — one bulk
    # query over the tournament's claimed seats, matched to advancer rows in
    # Python (never per-row queries). "Claimed" reads the §F1c ledger
    # directly, removed seats included: the ledger is the truth (a kicked
    # claimed seat still spent its claim; C-6's join-by-code is the recovery).
    advancers = serializers.SerializerMethodField()

    class Meta(TournamentSerializer.Meta):
        fields = TournamentSerializer.Meta.fields + ("games", "advancers")

    def get_advancers(self, tournament):
        rows = list(tournament.advancers.all())
        claimed_pairs = set(
            Participant.objects.filter(
                claimed_from__isnull=False, game__tournament=tournament
            ).values_list("game_id", "claimed_from_id")
        )
        data = TournamentAdvancerSerializer(rows, many=True).data
        for row, advancer in zip(data, rows):
            row["claimed"] = (
                advancer.target_game_id is not None
                and (advancer.target_game_id, advancer.source_participant_id) in claimed_pairs
            )
        return data


class JoinGameSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=50)


# --- Game history + report (Handoff #6 §G2) --------------------------------
# Purpose-built read serializers for finished/past games. Deliberately
# separate from the live-snapshot serializers above (which stay untouched).


class GameHistorySerializer(serializers.ModelSerializer):
    """One row in GET /api/games/history/ (host's own games, newest-first).

    `participant_count` counts player seats (the host's control seat is not a
    team). The view annotates it as `player_count` to avoid an N+1."""

    winners = serializers.SerializerMethodField()
    participant_count = serializers.SerializerMethodField()

    class Meta:
        model = Game
        fields = ("code", "mode", "status", "created_at", "finished_at", "winners", "participant_count")

    def get_winners(self, game):
        return sorted(w.name for w in game.winners.all())

    def get_participant_count(self, game):
        count = getattr(game, "player_count", None)
        if count is None:  # direct serializer use without the annotation
            # §F (#11): active players only, matching the view's annotation.
            count = game.participants.filter(role="player", removed_at__isnull=True).count()
        return count


class ReportQuestionSerializer(serializers.ModelSerializer):
    """A played (or unplayed) board cell in the post-game report — WITH the
    answer. Only ever serialized for finished games (the report view 409s
    otherwise), so rule 5 holds: no unrevealed answer can leak mid-game."""

    question_text = serializers.CharField(source="question.question_text", read_only=True)
    answer = serializers.CharField(source="question.answer", read_only=True)
    answered_by_name = serializers.CharField(source="answered_by.name", read_only=True, default=None)
    # §F1c (#21): the Thunder story for the finished report — ⚡, the shouted
    # wager, and who chugged ("<chugger> chugged Ns"; the frontend derives
    # "their own" when chugger == answered_by). ADDITIVE; this serializer is
    # already the post-finish, answer-bearing, host-private surface.
    is_thunder = serializers.BooleanField(read_only=True)
    thunder_wager = serializers.IntegerField(read_only=True)
    thunder_chugger_name = serializers.CharField(source="thunder_chugger.name", read_only=True, default=None)

    class Meta:
        model = BoardCell
        fields = (
            "id", "row", "value", "state", "question_text", "answer", "answered_by_name",
            "answered_correctly", "is_thunder", "thunder_wager", "thunder_chugger_name",
        )


class ReportColumnSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    questions = ReportQuestionSerializer(source="cells", many=True, read_only=True)

    class Meta:
        model = BoardColumn
        fields = ("id", "position", "category_name", "questions")


class ReportParticipantSerializer(ParticipantSerializer):
    """§F (Handoff #11): the host-private report keeps ALL participants —
    full history — and flags kicked seats so the frontend can badge them.
    The live snapshot's `participants` (above) excludes removed seats
    entirely; this serializer never rides a snapshot."""

    removed = serializers.SerializerMethodField()

    class Meta(ParticipantSerializer.Meta):
        fields = ParticipantSerializer.Meta.fields + ("removed",)

    def get_removed(self, participant):
        return participant.removed_at is not None


class GameReportSerializer(serializers.ModelSerializer):
    """GET /api/games/{code}/report/ — full post-game detail, host-only."""

    participants = ReportParticipantSerializer(many=True, read_only=True)
    winners = serializers.SerializerMethodField()
    columns = ReportColumnSerializer(many=True, read_only=True)

    class Meta:
        model = Game
        fields = (
            "code", "mode", "status", "questions_per_category",
            "created_at", "started_at", "finished_at",
            "participants", "winners", "columns",
        )

    def get_winners(self, game):
        # ids + names so the frontend can highlight rows without name-matching.
        return [{"id": w.id, "name": w.name} for w in game.winners.all()]


# --- Host-private lobby board detail (Handoff #8 §J3) -----------------------
# GET /api/games/{code}/board/ — the host previewing THEIR OWN board before
# starting, with question text + answer + question id per cell (the id feeds
# the Replace and flag actions). Host-only over Knox, exactly the §F1
# answer-endpoint pattern extended to the whole board; rule 5 holds because
# nothing here ever rides the public snapshot or a WS payload.


class BoardDetailCellSerializer(serializers.ModelSerializer):
    question_id = serializers.IntegerField(read_only=True)
    question_text = serializers.CharField(source="question.question_text", read_only=True)
    answer = serializers.CharField(source="question.answer", read_only=True)
    difficulty = serializers.IntegerField(source="question.difficulty", read_only=True)
    media_type = serializers.CharField(source="question.media_type", read_only=True)
    # §F1 (#21): the host may see WHERE the ⚡ sit (this surface is
    # host-private Knox — it is exactly where the §B no-spoiler pin says
    # thunder flags MAY ride; the crib-sheet PDF marks them the same way).
    is_thunder = serializers.BooleanField(read_only=True)

    class Meta:
        model = BoardCell
        fields = ("id", "row", "value", "state", "question_id", "question_text", "answer", "difficulty", "media_type", "is_thunder")


class BoardDetailColumnSerializer(serializers.ModelSerializer):
    # §F (Handoff #16): category_id joins the host-private board detail so
    # the lobby preview can seed the swap picker's "already on the board"
    # exclusions. Additive; this shape is host-only (never a snapshot).
    category_id = serializers.IntegerField(read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    cells = BoardDetailCellSerializer(many=True, read_only=True)

    class Meta:
        model = BoardColumn
        fields = ("id", "position", "category_id", "category_name", "cells")


class ColumnCategoryReplaceSerializer(serializers.Serializer):
    """§F (Handoff #16): the column-swap body — one category id. Deliberately
    id-based and theme-unaware, like game creation (§G #10 stands for board
    edits too)."""

    category_id = serializers.IntegerField(min_value=1)


class BoardDetailSerializer(serializers.ModelSerializer):
    columns = BoardDetailColumnSerializer(many=True, read_only=True)

    class Meta:
        model = Game
        fields = ("code", "mode", "status", "questions_per_category", "columns")
