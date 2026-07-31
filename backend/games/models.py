import secrets
import string

from django.conf import settings
from django.db import models

from trivia.models import Category, Question

CODE_ALPHABET = string.ascii_uppercase + string.digits


def generate_game_code() -> str:
    # Unambiguous-ish 6-char join code shown on the projected screen.
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def generate_participant_token() -> str:
    return secrets.token_urlsafe(24)


class GameMode(models.TextChoices):
    DRINKS = "drinks", "Drinks"
    POINTS = "points", "Points"


class GameStatus(models.TextChoices):
    LOBBY = "lobby", "Lobby"
    ACTIVE = "active", "Active"
    FINISHED = "finished", "Finished"


class Game(models.Model):
    code = models.CharField(max_length=8, unique=True, default=generate_game_code, db_index=True)
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hosted_games")
    mode = models.CharField(max_length=10, choices=GameMode.choices, default=GameMode.DRINKS)
    status = models.CharField(max_length=10, choices=GameStatus.choices, default=GameStatus.LOBBY)
    questions_per_category = models.PositiveSmallIntegerField(default=5)

    # §H (Handoff #13): the buzz sound is a HOST choice, per GAME — every
    # buzzer and the board play THIS sound (the four existing synthesized
    # WebAudio voices in lib/sounds.js; no audio files, ever — §M). Chosen at
    # creation only (mid-game change is punted, §M); rides the snapshot as
    # top-level `buzz_sound` so both transports render it (C2). Pre-#13 games
    # migrate to 1 (the classic buzzer). Participant.buzzer_sound survives
    # one session as a vestigial payload field (C12) — nothing reads it now.
    buzz_sound = models.PositiveSmallIntegerField(
        choices=[(i, f"Sound {i}") for i in (1, 2, 3, 4)], default=1
    )

    # Live buzzer state — persisted so a page reload recovers everything.
    current_cell = models.ForeignKey(
        "BoardCell", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    buzzer_open = models.BooleanField(default=False)
    # Answer-secrecy invariant (Handoff #6 §F2 / rule 5): True only between the
    # host's reveal and close_cell. While True, the public snapshot exposes the
    # open cell's answer via `revealed_answer` — the ONE sanctioned way an
    # answer enters a snapshot, and only ever post-reveal. Cleared on open_cell
    # (a fresh cell always starts unrevealed) and close_cell.
    answer_revealed = models.BooleanField(default=False)

    # §F (Handoff #8): the transient judgment marker behind the snapshot's
    # `current_cell.last_judgment`. Set when the host judges a buzz; cleared
    # (both fields) at every point the verdict stops being current: open_cell,
    # close_cell, reset_buzzer, an explicit host open_buzzer, the NEXT buzz
    # (a new team answering supersedes the old verdict on screen), and
    # finish_game. Judge-wrong's automatic buzzer reopen does NOT clear it —
    # that's the moment the "✘ WRONG" banner must be visible. Because it lives
    # in the snapshot, WS boards, polling boards and phones all render it with
    # no hook changes (§B4).
    judged_participant = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    judged_correct = models.BooleanField(null=True, blank=True)

    # Outcome, computed once at finish_game (Handoff #6 §G1) — never derived
    # on read. Empty for abandoned games (never finished) and for finished
    # games that had no players. Ties are real: every top-scoring player is in.
    winners = models.ManyToManyField(
        "Participant", blank=True, related_name="won_games"
    )

    # §I (Handoff #13): tournament membership — BOTH null for a plain game
    # (every pre-#13 game migrates that way and behaves exactly as before).
    # A round is the set of the tournament's games sharing `round_number`;
    # there is deliberately NO Round table v1 (it would add joins for no v1
    # behavior — revisit in §M if rounds grow their own state). SET_NULL so
    # an admin hard-delete of a Tournament row can never take games with it
    # (normal deletion is soft — Tournament.deleted_at).
    tournament = models.ForeignKey(
        "Tournament", null=True, blank=True, on_delete=models.SET_NULL, related_name="games"
    )
    round_number = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Game {self.code} ({self.get_mode_display()})"


class BoardColumn(models.Model):
    """A category slot on the board, in display order."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="columns")
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=["game", "position"], name="unique_column_position"),
            models.UniqueConstraint(fields=["game", "category"], name="unique_column_category"),
        ]


class CellState(models.TextChoices):
    HIDDEN = "hidden", "Hidden"
    OPEN = "open", "Open"
    ANSWERED = "answered", "Answered"


class BoardCell(models.Model):
    """One question tile: column x row, worth `value` drinks or points."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="cells")
    column = models.ForeignKey(BoardColumn, on_delete=models.CASCADE, related_name="cells")
    question = models.ForeignKey(Question, on_delete=models.PROTECT)
    row = models.PositiveSmallIntegerField()  # 0 = top (easiest)
    value = models.PositiveIntegerField()  # drinks count or point value
    state = models.CharField(max_length=10, choices=CellState.choices, default=CellState.HIDDEN)
    answered_by = models.ForeignKey(
        "Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="answered_cells"
    )
    answered_correctly = models.BooleanField(null=True, blank=True)
    # §G (Handoff #8): one drink assignment per cell, server-enforced. The
    # check-and-set happens inside services.assign_drink's transaction (row
    # lock on the Game); a second attempt — from the winner's phone OR the
    # host fallback — gets the structured `drinks_already_assigned` error.
    # First assignment wins, whichever side it came from.
    drinks_assigned = models.BooleanField(default=False)

    class Meta:
        ordering = ("column__position", "row")
        constraints = [
            models.UniqueConstraint(fields=["column", "row"], name="unique_cell_per_column_row"),
        ]


class ParticipantRole(models.TextChoices):
    HOST = "host", "Host"
    PLAYER = "player", "Player/Team buzzer"


class Participant(models.Model):
    """A team's designated buzzer holder (or the host's control connection).

    Players don't need accounts: they join with a name + game code and get a
    token stored client-side, so a reload reconnects them to the same seat.
    """

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="participants")
    name = models.CharField(max_length=50)
    role = models.CharField(max_length=10, choices=ParticipantRole.choices, default=ParticipantRole.PLAYER)
    token = models.CharField(max_length=64, unique=True, default=generate_participant_token)
    score = models.IntegerField(default=0)  # points in points mode
    drinks_taken = models.PositiveIntegerField(default=0)
    drinks_given = models.PositiveIntegerField(default=0)
    # §I: which of the 4 buzzer sounds this seat plays (1–4). Auto-assigned at
    # join round-robin by current player count — (player_count % 4) + 1 — so
    # the first four teams all sound different. The host seat gets one too but
    # never plays it. Players don't pick (punted). Display-only: no game
    # logic keys off this field.
    buzzer_sound = models.PositiveSmallIntegerField(default=1)
    # §F (Handoff #11): soft removal — the host kicked this seat. Null =
    # active; the ONLY liveness flag (same convention as Question/Category
    # `deleted_at` — a participant is REMOVED from a live game, not deleted
    # content, hence the name). The row is deliberately kept: Buzz and
    # DrinkAssignment CASCADE off it, BoardCell.answered_by is SET_NULL, and
    # Game.winners references it — a hard delete would erase buzz/drink
    # history and blank exactly the attribution §G renders. "Active" surfaces
    # (snapshot participants, join cap, token rejoin, WS connect + per-message
    # checks, winners, drink targets, judge lookups, history counts) filter
    # removed_at__isnull=True; history surfaces (the finished report, cell
    # attribution via the snapshot's `former_players`) keep the row.
    removed_at = models.DateTimeField(null=True, blank=True)
    connected = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # §F (Handoff #11): PARTIAL unique (active seats only) — third use
            # of the house pattern (unique_category_name_per_owner,
            # unique_active_theme_name) — so a kicked troll's name, or a
            # legitimately-rejoining team's, is immediately reusable.
            models.UniqueConstraint(
                fields=["game", "name"],
                condition=models.Q(removed_at__isnull=True),
                name="unique_participant_name_per_game",
            ),
        ]

    def __str__(self):
        return f"{self.name} in {self.game.code}"


class Buzz(models.Model):
    """One buzz event for one open cell. Ordering = server receive time.

    Stored in the DB (not just memory) so the buzz order survives reloads
    and every client can be replayed the full ordered list.
    """

    cell = models.ForeignKey(BoardCell, on_delete=models.CASCADE, related_name="buzzes")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="buzzes")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            # One buzz per team per question round.
            models.UniqueConstraint(fields=["cell", "participant"], name="one_buzz_per_participant_per_cell"),
        ]


class DrinkAssignment(models.Model):
    """History: who made whom drink, and for which question."""

    cell = models.ForeignKey(BoardCell, on_delete=models.CASCADE, related_name="drink_assignments")
    from_participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="drinks_dealt")
    to_participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="drinks_received")
    amount = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


# --- §I (Handoff #13): tournament mode v1 ----------------------------------
# A tournament is a NAMED, BRANDED container of rounds; each round is a set
# of ordinary games (Game.tournament + Game.round_number above); advancement
# is computed from finished games' standings and CONFIRMED by the host (the
# advance endpoint). No auto-seeding, no elimination trees, no cross-venue
# anything (§M). Creating one is plan-gated through the quota choke point
# (accounts/quotas: "tournaments", 0 for free) — the categories/questions
# pattern, so a staff limit_overrides grant works too.


class Tournament(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tournaments"
    )
    name = models.CharField(max_length=80)
    # Optional venue line ("Ian's Bar Venue"). The snapshot's hosted-by line
    # falls back to the owner's brand_name, then display_name (§I3 — resolved
    # SERVER-side in the serializer so clients stay dumb renderers).
    location = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)  # null = live
    # The house liveness flag (deleted_at on Question/Category, removed_at on
    # Participant): null = active, the ONLY flag. Soft delete keeps history
    # (attached games keep rendering their reports) and frees the name via
    # the partial constraint below.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # The FOURTH house partial unique (after category/theme/
            # participant names): live tournaments are unique per owner by
            # name; soft delete frees the name immediately. Needs the owner's
            # Postgres compose pass like the other three (C3).
            models.UniqueConstraint(
                fields=["owner", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_tournament_name_per_owner",
            ),
        ]

    def __str__(self):
        return f"Tournament {self.name} ({self.owner_id})"


class TournamentAdvancer(models.Model):
    """§I2: one advancing NAME from one round game.

    Names, not Participant FKs, on purpose: buzzer seats are per-game and
    anonymous — advancing "TEAM A" means the NAME goes through and re-joins
    the next round's game by typing it. Cheap, honest, and exactly how bars
    run this. `rank` is competition-style (1,1,3 on a tie); `source_game`
    says which game earned it. Rows for a round are REPLACED wholesale when
    the host re-runs advancement (services.advance_round)."""

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="advancers")
    round_number = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=50)
    source_game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="+")
    rank = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("round_number", "source_game_id", "rank", "name")
        constraints = [
            # Names are unique per game among active seats (the participant
            # partial), so one row per (round, game, name) is the honest
            # uniqueness — a tie can repeat a RANK, never a name.
            models.UniqueConstraint(
                fields=["tournament", "round_number", "source_game", "name"],
                name="unique_advancer_per_round_game_name",
            ),
        ]

    def __str__(self):
        return f"{self.name} → round {self.round_number + 1} of {self.tournament_id}"
