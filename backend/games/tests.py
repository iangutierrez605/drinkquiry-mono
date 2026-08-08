"""Game-logic regression suite (Handoff #6 §H) — the games app's first tests.

Covers §F1 (host-private answer endpoint), §F2 / rule 5 (persisted reveal in
the public snapshot, and the answer appearing NOWHERE pre-reveal), §G1
(outcome computed + stored at finish, ties, drinks-mode reading, abandoned
games) and §G2 (history scoping/ordering/route precedence, report auth and
the pinned 409-until-finished rule).

Live WS mutations under test (open/reveal/close/finish) share their bodies
with the consumer via games/services.py, so exercising the services here is
exercising exactly what the socket runs.

Run: `python manage.py test accounts trivia games` (and, per the
environment-parity rule, once through docker compose against Postgres before
shipping).
"""
import json
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from trivia.models import Category, Question

from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import BoardCell, BoardColumn, Game, GameStatus, Participant, ParticipantRole
from .models import Tournament, TournamentAdvancer
from .services import (
    ActionError,
    StructuredActionError,
    advance_round,
    assign_drink,
    close_cell,
    create_game,
    finalize_game,
    game_standings,
    judge_buzz,
    open_cell,
    register_buzz,
    reset_buzzer,
    reveal_answer,
    set_buzzer,
)


def seed_category(name: str, n_questions: int = 5) -> Category:
    """Official (owner-less) category with n usable questions; answers are
    distinctive strings we can grep entire payloads for."""
    cat = Category.objects.create(owner=None, name=name)
    for i in range(n_questions):
        question = Question.objects.create(
            owner=None,
            question_text=f"{name} question {i}?",
            answer=f"SECRET-{name}-{i}",
            difficulty=min(i + 1, 5),
        )
        question.categories.add(cat)  # §F (Handoff #10): categories are M2M
    return cat


def make_game(host, *, mode="points", n=2) -> Game:
    cat = seed_category(f"Cat-{host.pk}-{Game.objects.count()}", n_questions=n)
    return create_game(host=host, mode=mode, category_ids=[cat.id], questions_per_category=n)


class GameTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.host = User.objects.create_user(email="host@example.com", password="pw-12345678", display_name="Host")
        cls.rival = User.objects.create_user(email="rival@example.com", password="pw-12345678", display_name="Rival")

    def as_host(self):
        self.client.force_authenticate(self.host)

    def as_rival(self):
        self.client.force_authenticate(self.rival)

    def add_players(self, game, *names):
        return [Participant.objects.create(game=game, name=n) for n in names]

    def cells(self, game):
        return list(game.cells.order_by("column__position", "row"))


# ---------------------------------------------------------------------------
# §F1 — GET /api/games/<code>/answer/ (host-private, pre-reveal side channel)
# ---------------------------------------------------------------------------
class AnswerEndpointTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host)
        self.cell = self.cells(self.game)[0]

    def test_host_gets_open_cells_answer_exact_shape(self):
        open_cell(code=self.game.code, cell_id=self.cell.id)
        self.as_host()
        res = self.client.get(f"/api/games/{self.game.code}/answer/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(),
            {"question_id": self.cell.question_id, "answer": self.cell.question.answer},
        )

    def test_works_pre_reveal_thats_the_point(self):
        open_cell(code=self.game.code, cell_id=self.cell.id)
        self.game.refresh_from_db()
        self.assertFalse(self.game.answer_revealed)
        self.as_host()
        self.assertEqual(self.client.get(f"/api/games/{self.game.code}/answer/").status_code, 200)

    def test_second_knox_user_403(self):
        open_cell(code=self.game.code, cell_id=self.cell.id)
        self.as_rival()
        res = self.client.get(f"/api/games/{self.game.code}/answer/")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("SECRET-", json.dumps(res.json()))

    def test_unauthenticated_401(self):
        open_cell(code=self.game.code, cell_id=self.cell.id)
        self.assertEqual(self.client.get(f"/api/games/{self.game.code}/answer/").status_code, 401)

    def test_no_open_cell_409(self):
        self.as_host()
        res = self.client.get(f"/api/games/{self.game.code}/answer/")
        self.assertEqual(res.status_code, 409)
        self.assertIn("detail", res.json())

    def test_unknown_code_404(self):
        self.as_host()
        self.assertEqual(self.client.get("/api/games/ZZZZ99/answer/").status_code, 404)


# ---------------------------------------------------------------------------
# §F2 / rule 5 — revealed_answer in the public snapshot ("the money tests")
# ---------------------------------------------------------------------------
class SnapshotRevealTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host)
        self.c1, self.c2 = self.cells(self.game)

    def snapshot(self):
        res = self.client.get(f"/api/games/{self.game.code}/")  # unauthenticated, board-polling path
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_null_before_reveal_and_answer_nowhere_in_payload(self):
        open_cell(code=self.game.code, cell_id=self.c1.id)
        snap = self.snapshot()
        self.assertIsNone(snap["revealed_answer"])
        # The invariant, asserted against the whole serialized payload — not
        # just the field: no answer string may appear anywhere pre-reveal.
        self.assertNotIn("SECRET-", json.dumps(snap))
        self.assertNotIn("answer", snap["current_cell"])  # OpenCellSerializer untouched

    def test_holds_answer_between_reveal_and_close(self):
        open_cell(code=self.game.code, cell_id=self.c1.id)
        returned = reveal_answer(code=self.game.code)
        self.assertEqual(returned, self.c1.question.answer)  # legacy WS event payload unchanged
        self.assertEqual(self.snapshot()["revealed_answer"], self.c1.question.answer)

    def test_null_after_close_and_for_next_opened_cell(self):
        open_cell(code=self.game.code, cell_id=self.c1.id)
        reveal_answer(code=self.game.code)
        close_cell(code=self.game.code)
        snap = self.snapshot()
        self.assertIsNone(snap["revealed_answer"])
        self.assertNotIn("SECRET-", json.dumps(snap))
        # Next cell opens clean: no reveal carried over, its answer absent.
        open_cell(code=self.game.code, cell_id=self.c2.id)
        snap = self.snapshot()
        self.assertIsNone(snap["revealed_answer"])
        self.assertNotIn("SECRET-", json.dumps(snap))

    def test_reveal_without_open_cell_rejected(self):
        with self.assertRaises(ActionError):
            reveal_answer(code=self.game.code)


# ---------------------------------------------------------------------------
# §G1 — outcome computed and stored at finish
# ---------------------------------------------------------------------------
class FinalizeGameTests(GameTestBase):
    def test_finish_stores_finished_at_and_single_winner(self):
        game = make_game(self.host, mode="points")
        a, b = self.add_players(game, "Alpha", "Beta")
        a.score = 300
        a.save(update_fields=["score"])
        finalize_game(code=game.code)
        game.refresh_from_db()
        self.assertEqual(game.status, GameStatus.FINISHED)
        self.assertIsNotNone(game.finished_at)
        self.assertEqual([w.id for w in game.winners.all()], [a.id])

    def test_tie_stores_all_tied_winners(self):
        game = make_game(self.host, mode="points")
        a, b = self.add_players(game, "Alpha", "Beta")
        Participant.objects.filter(pk__in=[a.pk, b.pk]).update(score=200)
        finalize_game(code=game.code)
        self.assertEqual(
            sorted(w.name for w in game.winners.all()), ["Alpha", "Beta"]
        )

    def test_drinks_mode_winner_is_highest_score_credits(self):
        # Documented reading (CHANGES.md): in drinks mode `score` is the
        # credit side — the consumer adds cell.value to the correct answerer's
        # score when drinks are assigned, so highest score == most drinks
        # dealt. drinks_taken is the penalty side and never decides.
        game = make_game(self.host, mode="drinks")
        a, b = self.add_players(game, "Alpha", "Beta")
        a.score, a.drinks_given = 3, 3
        a.save(update_fields=["score", "drinks_given"])
        b.drinks_taken = 10  # suffered most — still not the winner
        b.save(update_fields=["drinks_taken"])
        finalize_game(code=game.code)
        self.assertEqual([w.id for w in game.winners.all()], [a.id])

    def test_abandoned_game_has_no_outcome(self):
        game = make_game(self.host)
        self.add_players(game, "Alpha")
        # Never finished: no finished_at, no winners — history must cope.
        game.refresh_from_db()
        self.assertIsNone(game.finished_at)
        self.assertEqual(game.winners.count(), 0)

    def test_finalize_is_idempotent(self):
        game = make_game(self.host, mode="points")
        (a,) = self.add_players(game, "Alpha")
        finalize_game(code=game.code)
        first_finished_at = Game.objects.get(pk=game.pk).finished_at
        a.score = -50  # late mutation must not rewrite the stored outcome
        a.save(update_fields=["score"])
        finalize_game(code=game.code)
        game.refresh_from_db()
        self.assertEqual(game.finished_at, first_finished_at)
        self.assertEqual([w.id for w in game.winners.all()], [a.id])

    def test_finished_game_with_no_players_stores_no_winners(self):
        game = make_game(self.host)
        finalize_game(code=game.code)
        self.assertEqual(game.winners.count(), 0)

    def test_finalize_clears_reveal_state(self):
        game = make_game(self.host)
        cell = self.cells(game)[0]
        open_cell(code=game.code, cell_id=cell.id)
        reveal_answer(code=game.code)
        finalize_game(code=game.code)
        game.refresh_from_db()
        self.assertFalse(game.answer_revealed)


# ---------------------------------------------------------------------------
# §G2 — GET /api/games/history/
# ---------------------------------------------------------------------------
class HistoryEndpointTests(GameTestBase):
    def test_scoped_to_requester_and_newest_first(self):
        g1 = make_game(self.host)
        g2 = make_game(self.host, mode="drinks")
        other = make_game(self.rival)
        self.as_host()
        res = self.client.get("/api/games/history/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("results", body)  # DRF-paginated
        codes = [g["code"] for g in body["results"]]
        self.assertEqual(codes, [g2.code, g1.code])  # newest-first
        self.assertNotIn(other.code, codes)

    def test_row_shape_winners_and_participant_count(self):
        game = make_game(self.host, mode="points")
        a, b = self.add_players(game, "Alpha", "Beta")
        a.score = 100
        a.save(update_fields=["score"])
        finalize_game(code=game.code)
        self.as_host()
        row = self.client.get("/api/games/history/").json()["results"][0]
        self.assertEqual(
            set(row),
            {"code", "mode", "status", "created_at", "finished_at", "winners", "participant_count"},
        )
        self.assertEqual(row["status"], "finished")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["winners"], ["Alpha"])
        self.assertEqual(row["participant_count"], 2)  # players only; host seat excluded

    def test_abandoned_game_row_has_no_winner(self):
        game = make_game(self.host)
        self.add_players(game, "Alpha")
        self.as_host()
        row = self.client.get("/api/games/history/").json()["results"][0]
        self.assertEqual(row["status"], "lobby")
        self.assertIsNone(row["finished_at"])
        self.assertEqual(row["winners"], [])

    def test_history_path_not_swallowed_by_code_lookup(self):
        # If games/<code>/ matched first, this would be a public-snapshot 404
        # for game "HISTORY". It must instead hit the authenticated list view:
        # 401 without credentials, 200 with them (even with zero games).
        self.assertEqual(self.client.get("/api/games/history/").status_code, 401)
        self.as_rival()
        res = self.client.get("/api/games/history/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["results"], [])

    def test_unauthenticated_401(self):
        self.assertEqual(self.client.get("/api/games/history/").status_code, 401)


# ---------------------------------------------------------------------------
# §G2 — GET /api/games/<code>/report/
# ---------------------------------------------------------------------------
class ReportEndpointTests(GameTestBase):
    def finished_game(self):
        game = make_game(self.host, mode="drinks")
        a, b = self.add_players(game, "Alpha", "Beta")
        a.score, a.drinks_given = 2, 2
        a.save(update_fields=["score", "drinks_given"])
        b.drinks_taken = 2
        b.save(update_fields=["drinks_taken"])
        finalize_game(code=game.code)
        return game, a, b

    def test_unauthenticated_401(self):
        game, *_ = self.finished_game()
        self.assertEqual(self.client.get(f"/api/games/{game.code}/report/").status_code, 401)

    def test_non_host_knox_user_403_no_leak(self):
        game, *_ = self.finished_game()
        self.as_rival()
        res = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("SECRET-", json.dumps(res.json()))

    def test_unknown_code_404(self):
        self.as_host()
        self.assertEqual(self.client.get("/api/games/ZZZZ99/report/").status_code, 404)

    def test_non_finished_409_pinned(self):
        # The chosen behavior for in-progress games (Handoff #6 §G2): 409, so
        # a live game's report can never exist to leak unrevealed answers.
        game = make_game(self.host)
        cell = self.cells(game)[0]
        open_cell(code=game.code, cell_id=cell.id)  # mid-question, unrevealed
        self.as_host()
        res = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(res.status_code, 409)
        self.assertNotIn("SECRET-", json.dumps(res.json()))

    def test_finished_report_full_detail_with_answers(self):
        game, a, b = self.finished_game()
        self.as_host()
        res = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], game.code)
        self.assertEqual(body["status"], "finished")
        self.assertEqual([w["name"] for w in body["winners"]], ["Alpha"])
        pmap = {p["name"]: p for p in body["participants"]}
        self.assertEqual(pmap["Alpha"]["drinks_given"], 2)
        self.assertEqual(pmap["Beta"]["drinks_taken"], 2)
        questions = [q for col in body["columns"] for q in col["questions"]]
        self.assertEqual(len(questions), 2)
        for q in questions:
            self.assertIn("question_text", q)
            self.assertTrue(q["answer"].startswith("SECRET-"))


# ---------------------------------------------------------------------------
# Handoff #7 §G — player cap (6), atomic join, and §I — buzzer sound assignment
# ---------------------------------------------------------------------------
class JoinCapAndSoundTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host)
        # The host's control seat, exactly as GameCreateView creates it — it
        # must NOT count against the player cap.
        self.host_seat = Participant.objects.create(
            game=self.game, name="Host", role=ParticipantRole.HOST
        )

    def join(self, name, token=None):
        body = {"name": name}
        if token:
            body["participant_token"] = token
        return self.client.post(f"/api/games/{self.game.code}/join/", body, format="json")

    def fill_to_cap(self):
        responses = [self.join(f"Team {i}") for i in range(1, 7)]
        for r in responses:
            self.assertEqual(r.status_code, 201, r.data)
        return responses

    def test_sixth_join_succeeds_seventh_gets_exact_409_shape(self):
        self.fill_to_cap()
        res = self.join("Team 7")
        self.assertEqual(res.status_code, 409, res.data)
        body = res.json()
        # NEW contract, separate from the quota 403s: exactly these keys.
        self.assertEqual(set(body), {"detail", "code", "limit"})
        self.assertEqual(body["code"], "game_full")
        self.assertEqual(body["limit"], 6)
        self.assertIsInstance(body["limit"], int)  # Response(), not coerced exception detail
        self.assertEqual(self.game.participants.filter(role=ParticipantRole.PLAYER).count(), 6)

    def test_host_seat_excluded_from_the_count(self):
        # 5 players + the host seat: a 6th player must still fit.
        for i in range(1, 6):
            self.assertEqual(self.join(f"Team {i}").status_code, 201)
        self.assertEqual(self.join("Team 6").status_code, 201)
        self.assertEqual(self.join("Team 7").status_code, 409)

    def test_rejoin_with_valid_token_returns_200_at_cap(self):
        first = self.fill_to_cap()[0].json()
        res = self.join("Team 1", token=first["participant_token"])
        self.assertEqual(res.status_code, 200, res.data)  # the reload flow survives a full table
        self.assertEqual(res.json()["participant"]["id"], first["participant"]["id"])

    def test_name_taken_400_unchanged_below_cap(self):
        self.assertEqual(self.join("Team A").status_code, 201)
        res = self.join("Team A")
        self.assertEqual(res.status_code, 400)
        self.assertIn("name", res.json())

    def test_snapshot_exposes_max_players(self):
        res = self.client.get(f"/api/games/{self.game.code}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["max_players"], 6)

    # --- §I: round-robin sound by join order -------------------------------

    def test_round_robin_assignment_wraps_after_four(self):
        sounds = [self.join(f"Team {i}").json()["participant"]["buzzer_sound"] for i in range(1, 6)]
        self.assertEqual(sounds, [1, 2, 3, 4, 1])

    def test_buzzer_sound_in_snapshot_participants_and_join_response(self):
        joined = self.join("Team A").json()
        self.assertIn("buzzer_sound", joined["participant"])
        snap = self.client.get(f"/api/games/{self.game.code}/").json()
        by_name = {p["name"]: p for p in snap["participants"]}
        # §H1 (Handoff #8): joins are uppercased server-side, so the snapshot
        # carries "TEAM A" for a "Team A" join.
        self.assertEqual(by_name["TEAM A"]["buzzer_sound"], 1)
        self.assertIn("buzzer_sound", by_name["Host"])  # host seat has one, never plays it


# ---------------------------------------------------------------------------
# Handoff #13 §H — the buzz sound is a HOST choice, per GAME
# ---------------------------------------------------------------------------
class GameBuzzSoundTests(GameTestBase):
    """§H: Game.buzz_sound (1–4, default 1) — set at creation only, rides the
    snapshot top-level so buzzers AND the board play the same sound. The
    per-participant `buzzer_sound` stays in payloads untouched this session
    (C12 — its tests above keep pinning it; removal is §M)."""

    def setUp(self):
        self.cat = seed_category("Soundcat")
        self.as_host()

    def create_via_api(self, **extra):
        body = {"mode": "drinks", "categories": [self.cat.id], "questions_per_category": 2, **extra}
        return self.client.post("/api/games/", body, format="json")

    def test_create_with_each_valid_sound(self):
        for sound in (1, 2, 3, 4):
            res = self.create_via_api(buzz_sound=sound)
            self.assertEqual(res.status_code, 201, res.data)
            code = res.json()["game"]["code"]
            self.assertEqual(Game.objects.get(code=code).buzz_sound, sound)
            # And the create response's own snapshot already carries it.
            self.assertEqual(res.json()["game"]["buzz_sound"], sound)

    def test_invalid_sounds_400(self):
        for bad in (0, 5, "x"):
            res = self.create_via_api(buzz_sound=bad)
            self.assertEqual(res.status_code, 400, (bad, res.data))
            self.assertIn("buzz_sound", res.json())
        self.assertEqual(Game.objects.count(), 0)  # nothing half-created

    def test_default_is_1_when_omitted(self):
        res = self.create_via_api()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.json()["game"]["buzz_sound"], 1)

    def test_service_validates_too(self):
        # rule 4 lives where the mutation lives — the suite (and the socket)
        # call services directly, so the serializer alone is not the gate.
        with self.assertRaises(DRFValidationError):
            create_game(host=self.host, mode="drinks", category_ids=[self.cat.id],
                        questions_per_category=2, buzz_sound=7)

    def test_snapshot_carries_top_level_buzz_sound(self):
        game = create_game(host=self.host, mode="drinks", category_ids=[self.cat.id],
                           questions_per_category=2, buzz_sound=3)
        snap = self.client.get(f"/api/games/{game.code}/").json()
        self.assertEqual(snap["buzz_sound"], 3)

    def test_pre_13_game_defaults_to_1(self):
        # Forward-path sanity in-suite: a Game row created without the field
        # (exactly what the migration leaves behind for old games) snapshots
        # buzz_sound=1. The pristine-tree migration run (C9) covers the real
        # thing; this pins the default the snapshot serves.
        game = make_game(self.host)
        snap = self.client.get(f"/api/games/{game.code}/").json()
        self.assertEqual(snap["buzz_sound"], 1)


# ---------------------------------------------------------------------------
# Handoff #8 §F — judging performs the reveal; the snapshot judgment marker
# ---------------------------------------------------------------------------
class JudgmentFlowTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host, mode="drinks", n=2)
        self.c1, self.c2 = self.cells(self.game)
        self.a, self.b = self.add_players(self.game, "TEAM A", "TEAM B")
        open_cell(code=self.game.code, cell_id=self.c1.id)

    def snapshot(self):
        res = self.client.get(f"/api/games/{self.game.code}/")  # unauthenticated board path
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_judge_correct_reveals_and_sets_marker(self):
        judge_buzz(code=self.game.code, participant_id=self.b.id, correct=True)
        snap = self.snapshot()
        # The reveal arrives WITH the judgment — no separate reveal action —
        # through the one sanctioned vehicle (revealed_answer, rule 5).
        self.assertEqual(snap["revealed_answer"], self.c1.question.answer)
        self.assertEqual(
            snap["current_cell"]["last_judgment"],
            {"participant_id": self.b.id, "name": "TEAM B", "correct": True},
        )
        self.assertFalse(snap["buzzer_open"])  # correct locks the buzzer, as before

    def test_judge_wrong_does_not_reveal_and_marker_survives_the_auto_reopen(self):
        judge_buzz(code=self.game.code, participant_id=self.a.id, correct=False)
        snap = self.snapshot()
        self.assertIsNone(snap["revealed_answer"])  # question still live for other teams
        self.assertNotIn("SECRET-", json.dumps(snap))
        self.assertEqual(
            snap["current_cell"]["last_judgment"],
            {"participant_id": self.a.id, "name": "TEAM A", "correct": False},
        )
        # Judge-wrong's automatic reopen must NOT clear the verdict — that's
        # the moment the "✘ WRONG" banner is on screen.
        self.assertTrue(snap["buzzer_open"])

    def test_marker_clears_at_close_and_next_cell_opens_clean(self):
        judge_buzz(code=self.game.code, participant_id=self.b.id, correct=True)
        close_cell(code=self.game.code)
        self.game.refresh_from_db()
        self.assertIsNone(self.game.judged_participant_id)
        self.assertIsNone(self.game.judged_correct)
        open_cell(code=self.game.code, cell_id=self.c2.id)
        snap = self.snapshot()
        self.assertIsNone(snap["current_cell"]["last_judgment"])
        self.assertIsNone(snap["revealed_answer"])  # reveal never outlives its cell either

    def test_marker_clears_on_reset_buzzer(self):
        judge_buzz(code=self.game.code, participant_id=self.a.id, correct=False)
        reset_buzzer(code=self.game.code)
        self.assertIsNone(self.snapshot()["current_cell"]["last_judgment"])

    def test_marker_clears_on_explicit_host_reopen(self):
        judge_buzz(code=self.game.code, participant_id=self.a.id, correct=False)
        set_buzzer(code=self.game.code, is_open=True)
        self.assertIsNone(self.snapshot()["current_cell"]["last_judgment"])

    def test_marker_clears_when_the_next_team_buzzes(self):
        judge_buzz(code=self.game.code, participant_id=self.a.id, correct=False)  # auto-reopens
        register_buzz(code=self.game.code, participant=self.b)
        snap = self.snapshot()
        self.assertIsNone(snap["current_cell"]["last_judgment"])
        self.assertEqual(snap["current_cell"]["buzzes"][0]["participant_id"], self.b.id)

    def test_lock_buzzer_keeps_the_marker(self):
        judge_buzz(code=self.game.code, participant_id=self.a.id, correct=False)
        set_buzzer(code=self.game.code, is_open=False)  # host locks while the banner shows
        self.assertIsNotNone(self.snapshot()["current_cell"]["last_judgment"])

    def test_judge_correct_does_not_double_reveal(self):
        reveal_answer(code=self.game.code)  # host revealed first ("nobody got it"... then someone did)
        judge_buzz(code=self.game.code, participant_id=self.b.id, correct=True)
        self.assertEqual(self.snapshot()["revealed_answer"], self.c1.question.answer)


# ---------------------------------------------------------------------------
# Handoff #8 §G — one drink assignment per cell, from either side
# ---------------------------------------------------------------------------
class DrinkAssignmentTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host, mode="drinks", n=2)
        self.c1, self.c2 = self.cells(self.game)
        self.host_seat = Participant.objects.create(game=self.game, name="QUIZMASTER", role=ParticipantRole.HOST)
        self.a, self.b = self.add_players(self.game, "TEAM A", "TEAM B")
        open_cell(code=self.game.code, cell_id=self.c1.id)
        judge_buzz(code=self.game.code, participant_id=self.b.id, correct=True)

    def refresh(self, *objs):
        for o in objs:
            o.refresh_from_db()

    def snapshot(self):
        return self.client.get(f"/api/games/{self.game.code}/").json()

    def test_winner_assigns_from_their_phone(self):
        assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.a.id)
        self.refresh(self.a, self.b, self.c1)
        self.assertEqual(self.a.drinks_taken, self.c1.value)
        self.assertEqual(self.b.drinks_given, self.c1.value)
        self.assertEqual(self.b.score, self.c1.value)  # credit side unchanged
        self.assertTrue(self.c1.drinks_assigned)

    def test_second_attempt_rejected_with_exact_structured_shape_both_paths(self):
        assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.a.id)
        for actor in (self.b, self.host_seat):  # player path AND host fallback
            with self.assertRaises(StructuredActionError) as ctx:
                assign_drink(code=self.game.code, actor=actor, target_participant_id=self.a.id)
            payload = ctx.exception.payload
            self.assertEqual(set(payload), {"detail", "code"})  # lesson C4: exact shape
            self.assertEqual(payload["code"], "drinks_already_assigned")
        self.refresh(self.a)
        self.assertEqual(self.a.drinks_taken, self.c1.value)  # tally unchanged by the rejects

    def test_only_the_judged_correct_winner_may_give(self):
        with self.assertRaises(ActionError):
            assign_drink(code=self.game.code, actor=self.a, target_participant_id=self.b.id)
        self.refresh(self.c1)
        self.assertFalse(self.c1.drinks_assigned)

    def test_host_fallback_consumes_the_same_marker(self):
        assign_drink(code=self.game.code, actor=self.host_seat, target_participant_id=self.a.id)
        with self.assertRaises(StructuredActionError):
            assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.a.id)

    def test_host_seat_is_a_valid_target(self):
        assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.host_seat.id)
        self.refresh(self.host_seat, self.b)
        self.assertEqual(self.host_seat.drinks_taken, self.c1.value)
        self.assertEqual(self.b.drinks_given, self.c1.value)

    def test_self_assignment_allowed(self):
        assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.b.id)
        self.refresh(self.b)
        self.assertEqual(self.b.drinks_taken, self.c1.value)
        self.assertEqual(self.b.drinks_given, self.c1.value)
        self.assertEqual(self.b.score, self.c1.value)

    def test_snapshot_carries_assigned_state_and_attribution(self):
        pre = self.snapshot()["current_cell"]
        self.assertFalse(pre["drinks_assigned"])
        self.assertIsNone(pre["drink_assignment"])
        assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.host_seat.id)
        cell = self.snapshot()["current_cell"]
        self.assertTrue(cell["drinks_assigned"])
        self.assertEqual(
            cell["drink_assignment"],
            {
                "from_participant_id": self.b.id,
                "from_name": "TEAM B",
                "to_participant_id": self.host_seat.id,
                "to_name": "QUIZMASTER",
                "amount": self.c1.value,
            },
        )

    def test_requires_a_judged_correct_current_cell(self):
        close_cell(code=self.game.code)
        with self.assertRaises(ActionError):
            assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.a.id)
        open_cell(code=self.game.code, cell_id=self.c2.id)  # fresh cell, not judged yet
        with self.assertRaises(ActionError):
            assign_drink(code=self.game.code, actor=self.b, target_participant_id=self.a.id)

    def test_points_mode_has_no_drinks(self):
        game = make_game(self.host, mode="points", n=1)
        cell = self.cells(game)[0]
        a, b = self.add_players(game, "P1", "P2")
        open_cell(code=game.code, cell_id=cell.id)
        judge_buzz(code=game.code, participant_id=b.id, correct=True)
        with self.assertRaises(ActionError):
            assign_drink(code=game.code, actor=b, target_participant_id=a.id)


# ---------------------------------------------------------------------------
# Handoff #8 §H1 — team names are ALL CAPS, normalized server-side
# ---------------------------------------------------------------------------
class JoinNameNormalizationTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host)

    def join(self, name, token=None):
        body = {"name": name}
        if token:
            body["participant_token"] = token
        return self.client.post(f"/api/games/{self.game.code}/join/", body, format="json")

    def test_join_uppercases_server_side(self):
        res = self.join("quizzy mcguinness")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.json()["participant"]["name"], "QUIZZY MCGUINNESS")
        snap = self.client.get(f"/api/games/{self.game.code}/").json()
        self.assertIn("QUIZZY MCGUINNESS", {p["name"] for p in snap["participants"]})

    def test_case_variants_collide_as_the_same_name(self):
        self.assertEqual(self.join("team a").status_code, 201)
        res = self.join("TEAM A")
        self.assertEqual(res.status_code, 400)
        self.assertIn("name", res.json())
        res = self.join("Team A")  # and any other casing
        self.assertEqual(res.status_code, 400)

    def test_rejoin_by_token_for_preexisting_mixed_case_seat_still_200(self):
        # Seats from games older than §H1 keep mixed-case names; rejoin is by
        # token, never name equality, so nothing breaks.
        legacy = Participant.objects.create(game=self.game, name="MiXeD CaSe")
        res = self.join("anything at all", token=legacy.token)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.json()["participant"]["id"], legacy.id)
        self.assertEqual(res.json()["participant"]["name"], "MiXeD CaSe")


# ---------------------------------------------------------------------------
# Handoff #8 §I — resume: history lists unfinished games; host-seat recovery
# ---------------------------------------------------------------------------
class ResumeTests(GameTestBase):
    def setUp(self):
        self.lobby_game = make_game(self.host)
        self.active_game = make_game(self.host)
        Game.objects.filter(pk=self.active_game.pk).update(status=GameStatus.ACTIVE)
        self.finished_game = make_game(self.host)
        finalize_game(code=self.finished_game.code)
        self.host_seat = Participant.objects.create(
            game=self.lobby_game, name="Host", role=ParticipantRole.HOST
        )

    def test_history_includes_unfinished_games_flagged_by_status(self):
        self.as_host()
        res = self.client.get("/api/games/history/")
        self.assertEqual(res.status_code, 200)
        by_code = {g["code"]: g["status"] for g in res.json()["results"]}
        self.assertEqual(by_code[self.lobby_game.code], "lobby")
        self.assertEqual(by_code[self.active_game.code], "active")
        self.assertEqual(by_code[self.finished_game.code], "finished")

    def test_host_seat_recovery_returns_token_and_seat(self):
        self.as_host()
        res = self.client.get(f"/api/games/{self.lobby_game.code}/host-seat/")
        self.assertEqual(res.status_code, 200, res.data)
        body = res.json()
        self.assertEqual(set(body), {"participant", "participant_token"})
        self.assertEqual(body["participant_token"], self.host_seat.token)
        self.assertEqual(body["participant"]["id"], self.host_seat.id)
        self.assertEqual(body["participant"]["role"], "host")

    def test_host_seat_recovery_is_host_private(self):
        self.as_rival()
        res = self.client.get(f"/api/games/{self.lobby_game.code}/host-seat/")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn(self.host_seat.token, json.dumps(res.json()))
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/games/{self.lobby_game.code}/host-seat/").status_code, 401)

    def test_host_seat_recovery_unknown_code_404(self):
        self.as_host()
        self.assertEqual(self.client.get("/api/games/ZZZZ99/host-seat/").status_code, 404)


# ---------------------------------------------------------------------------
# Handoff #8 §J2 — the board draw prefers questions this host hasn't used
# ---------------------------------------------------------------------------
class DrawPreferenceTests(GameTestBase):
    def uniform_category(self, name, n, difficulty=1):
        cat = Category.objects.create(owner=None, name=name)
        questions = []
        for i in range(n):
            q = Question.objects.create(
                owner=None,
                question_text=f"{name} question {i}?", answer=f"SECRET-{name}-{i}",
                difficulty=difficulty,
            )
            q.categories.add(cat)
            questions.append(q)
        return cat, questions

    def question_ids(self, game):
        return set(game.cells.values_list("question_id", flat=True))

    def test_next_game_avoids_this_hosts_used_questions_when_alternatives_exist(self):
        cat, questions = self.uniform_category("Prefer", 4)
        first = create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        used = self.question_ids(first)
        second = create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        # 2 of 4 used, 2 untouched: the next draw must take the untouched 2.
        self.assertEqual(self.question_ids(second), {q.id for q in questions} - used)

    def test_falls_back_to_repeats_when_no_alternatives(self):
        cat, questions = self.uniform_category("Repeat", 2)
        create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        second = create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        self.assertEqual(self.question_ids(second), {q.id for q in questions})

    def test_other_hosts_usage_only_breaks_ties(self):
        cat, (q1, q2, q3) = self.uniform_category("Global", 3)
        # The rival used q1 and q2 in a game of their own (rows built directly
        # so the setup is deterministic).
        rival_game = Game.objects.create(host=self.rival, mode="drinks")
        column = BoardColumn.objects.create(game=rival_game, category=cat, position=0)
        BoardCell.objects.create(game=rival_game, column=column, question=q1, row=0, value=1)
        BoardCell.objects.create(game=rival_game, column=column, question=q2, row=1, value=2)
        # Our host has used nothing (host_uses ties at 0), so global usage is
        # the tiebreak: the globally-untouched q3 must be on the board.
        game = create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        self.assertIn(q3.id, self.question_ids(game))

    def test_shortage_contract_unchanged(self):
        cat, _ = self.uniform_category("Short", 2)
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=3)
        detail = ctx.exception.detail["categories"][0]
        self.assertIn("'Short' only has 2 usable question(s); 3 required.", str(detail))

    def test_difficulty_slotting_still_wins_over_preference(self):
        # A used easy question still beats an unused hard one for the top row.
        cat = Category.objects.create(owner=None, name="Slots")
        easy = Question.objects.create(owner=None, question_text="easy?", answer="A", difficulty=1)
        easy.categories.add(cat)
        hard = Question.objects.create(owner=None, question_text="hard?", answer="B", difficulty=5)
        hard.categories.add(cat)
        prior = Game.objects.create(host=self.host, mode="drinks")
        column = BoardColumn.objects.create(game=prior, category=cat, position=0)
        BoardCell.objects.create(game=prior, column=column, question=easy, row=0, value=1)
        game = create_game(host=self.host, mode="drinks", category_ids=[cat.id], questions_per_category=2)
        rows = {c.row: c.question_id for c in game.cells.all()}
        self.assertEqual(rows, {0: easy.id, 1: hard.id})


# ---------------------------------------------------------------------------
# Handoff #8 §J3 — host-private lobby board detail + per-cell replace
# ---------------------------------------------------------------------------
class BoardDetailAndReplaceTests(GameTestBase):
    def setUp(self):
        self.game = make_game(self.host, n=2)

    def test_board_detail_gives_the_host_questions_and_answers(self):
        self.as_host()
        res = self.client.get(f"/api/games/{self.game.code}/board/")
        self.assertEqual(res.status_code, 200, res.data)
        cells = [c for col in res.json()["columns"] for c in col["cells"]]
        self.assertEqual(len(cells), 2)
        for cell in cells:
            self.assertTrue(cell["question_text"])
            self.assertIn("SECRET-", cell["answer"])
            self.assertIn("question_id", cell)

    def test_board_detail_never_leaks_to_non_hosts(self):
        self.as_rival()
        res = self.client.get(f"/api/games/{self.game.code}/board/")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("SECRET-", json.dumps(res.json()))
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/games/{self.game.code}/board/").status_code, 401)

    def test_replace_redraws_same_category_closest_difficulty_excluding_board(self):
        cat = self.game.columns.first().category
        # Two spares: one at the outgoing question's difficulty, one far away.
        near = Question.objects.create(
            owner=None, question_text="near spare?", answer="SECRET-near", difficulty=1
        )
        near.categories.add(cat)
        far = Question.objects.create(
            owner=None, question_text="far spare?", answer="SECRET-far", difficulty=5
        )
        far.categories.add(cat)
        target = self.cells(self.game)[0]  # row 0 = difficulty 1 in seed_category
        self.assertEqual(target.question.difficulty, 1)
        on_board_before = set(self.game.cells.values_list("question_id", flat=True))
        self.as_host()
        res = self.client.post(f"/api/games/{self.game.code}/cells/{target.id}/replace/")
        self.assertEqual(res.status_code, 200, res.data)
        body = res.json()
        self.assertEqual(body["id"], target.id)
        self.assertEqual(body["question_id"], near.id)  # closest difficulty wins
        self.assertNotIn(body["question_id"], on_board_before)
        target.refresh_from_db()
        self.assertEqual(target.question_id, near.id)

    def test_replace_409_when_category_has_nothing_left(self):
        target = self.cells(self.game)[0]  # seed_category made exactly n questions; all on board
        self.as_host()
        res = self.client.post(f"/api/games/{self.game.code}/cells/{target.id}/replace/")
        self.assertEqual(res.status_code, 409)
        self.assertIn("detail", res.json())

    def test_replace_409_after_start(self):
        spare = Question.objects.create(
            owner=None, question_text="spare?", answer="SECRET-spare", difficulty=1,
        )
        spare.categories.add(self.game.columns.first().category)
        Game.objects.filter(pk=self.game.pk).update(status=GameStatus.ACTIVE)
        target = self.cells(self.game)[0]
        self.as_host()
        res = self.client.post(f"/api/games/{self.game.code}/cells/{target.id}/replace/")
        self.assertEqual(res.status_code, 409)
        self.assertIn("started", res.json()["detail"])

    def test_replace_is_host_private(self):
        target = self.cells(self.game)[0]
        self.as_rival()
        self.assertEqual(
            self.client.post(f"/api/games/{self.game.code}/cells/{target.id}/replace/").status_code, 403
        )
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.post(f"/api/games/{self.game.code}/cells/{target.id}/replace/").status_code, 401
        )


# ---------------------------------------------------------------------------
# Handoff #16 §F — lobby-only whole-column category swap
# ---------------------------------------------------------------------------
class ColumnCategorySwapTests(GameTestBase):
    """POST /api/games/<code>/columns/<column_id>/replace/ {category_id} —
    the cell-replace contract (lobby-only 409, host-only 403, ActionError →
    structured 409) extended to a whole column, plus the swap-specific
    rules: no duplicate category on the board, deleted categories are
    invisible, creation's shortage message format, dupe-free rebuild with
    creation's value scaling, and the post-swap exclusion semantics."""

    def setUp(self):
        # A 2-column board (3 rows) so cross-column exclusions are real.
        self.cat_a = seed_category("Alpha", n_questions=3)
        self.cat_b = seed_category("Bravo", n_questions=3)
        self.game = create_game(
            host=self.host, mode="drinks", category_ids=[self.cat_a.id, self.cat_b.id], questions_per_category=3
        )
        self.column_b = self.game.columns.get(category=self.cat_b)

    def swap(self, column_id, category_id, code=None):
        return self.client.post(
            f"/api/games/{code or self.game.code}/columns/{column_id}/replace/",
            {"category_id": category_id},
            format="json",
        )

    def board_question_ids(self):
        return list(self.game.cells.values_list("question_id", flat=True))

    def test_swap_rebuilds_the_column_from_the_new_category(self):
        fresh = seed_category("Charlie", n_questions=4)
        before_other = set(
            self.game.cells.filter(column__category=self.cat_a).values_list("question_id", flat=True)
        )
        self.as_host()
        res = self.swap(self.column_b.id, fresh.id)
        self.assertEqual(res.status_code, 200, res.data)
        body = res.json()
        # Board-detail column shape, patchable in place: same column id/
        # position, new category, full cells with questions AND answers.
        self.assertEqual(body["id"], self.column_b.id)
        self.assertEqual(body["position"], self.column_b.position)
        self.assertEqual(body["category_id"], fresh.id)
        self.assertEqual(body["category_name"], "Charlie")
        self.assertEqual(len(body["cells"]), 3)
        for cell in body["cells"]:
            self.assertTrue(cell["question_text"])
            self.assertIn("SECRET-Charlie-", cell["answer"])
        self.column_b.refresh_from_db()
        self.assertEqual(self.column_b.category_id, fresh.id)
        # Every rebuilt cell draws from Charlie; the Alpha column is untouched.
        new_ids = set(
            self.game.cells.filter(column=self.column_b).values_list("question_id", flat=True)
        )
        charlie_ids = set(fresh.questions.values_list("id", flat=True))
        self.assertTrue(new_ids <= charlie_ids)
        after_other = set(
            self.game.cells.filter(column__category=self.cat_a).values_list("question_id", flat=True)
        )
        self.assertEqual(before_other, after_other)
        # Dupe-free board, full size.
        ids = self.board_question_ids()
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)

    def test_swap_scales_values_by_row_like_creation(self):
        # Drinks mode: 1..n down the column (creation's _cell_value).
        fresh = seed_category("Delta", n_questions=3)
        self.as_host()
        self.assertEqual(self.swap(self.column_b.id, fresh.id).status_code, 200)
        rows = list(
            self.game.cells.filter(column=self.column_b).order_by("row").values_list("row", "value")
        )
        self.assertEqual(rows, [(0, 1), (1, 2), (2, 3)])
        # And easiest→hardest down the column, exactly like creation.
        difficulties = [
            c.question.difficulty
            for c in self.game.cells.filter(column=self.column_b).order_by("row").select_related("question")
        ]
        self.assertEqual(difficulties, sorted(difficulties))

    def test_swap_excludes_questions_kept_by_other_columns(self):
        # A question shared with the SURVIVING column must not be drawn twice:
        # give Charlie 3 own questions + 1 that also lives in Alpha (already
        # on the board). A 3-row rebuild must dodge the shared one.
        fresh = seed_category("Charlie", n_questions=3)
        shared = self.game.cells.filter(column__category=self.cat_a).first().question
        shared.categories.add(fresh)
        self.as_host()
        res = self.swap(self.column_b.id, fresh.id)
        self.assertEqual(res.status_code, 200, res.data)
        ids = self.board_question_ids()
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6, "a question landed on the board twice")

    def test_swap_may_reuse_a_question_shared_with_the_outgoing_column(self):
        # The exclusion set is the POST-SWAP board: a question shared by the
        # OUTGOING category and the incoming one is a legitimate pick — the
        # old copy dies in the same transaction. Charlie has exactly 3
        # usable questions and one of them currently sits in the Bravo
        # column being replaced; the literal whole-board exclusion would
        # 409 a perfectly fillable swap.
        fresh = seed_category("Charlie", n_questions=2)
        shared = self.game.cells.filter(column=self.column_b).first().question
        shared.categories.add(fresh)
        self.as_host()
        res = self.swap(self.column_b.id, fresh.id)
        self.assertEqual(res.status_code, 200, res.data)
        ids = self.board_question_ids()
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)
        self.assertIn(
            shared.id,
            self.game.cells.filter(column=self.column_b).values_list("question_id", flat=True),
        )

    def test_swap_to_category_already_on_the_board_409(self):
        self.as_host()
        before = self.board_question_ids()
        res = self.swap(self.column_b.id, self.cat_a.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("already on the board", res.json()["detail"])
        # The column's OWN category counts as "already on the board" too.
        self.assertEqual(self.swap(self.column_b.id, self.cat_b.id).status_code, 409)
        self.assertEqual(self.board_question_ids(), before)  # board unchanged

    def test_swap_to_deleted_category_409(self):
        gone = seed_category("Gone", n_questions=3)
        gone.deleted_at = timezone.now()
        gone.save(update_fields=["deleted_at"])
        self.as_host()
        res = self.swap(self.column_b.id, gone.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("Unknown category", res.json()["detail"])

    def test_swap_to_thin_category_409_with_creations_message_format(self):
        thin = seed_category("Thin", n_questions=1)  # needs 3
        self.as_host()
        before = self.board_question_ids()
        res = self.swap(self.column_b.id, thin.id)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(
            res.json()["detail"], "'Thin' only has 1 usable question(s); 3 required."
        )
        self.assertEqual(self.board_question_ids(), before)  # nothing half-torn

    def test_swap_409_after_start(self):
        fresh = seed_category("Late", n_questions=3)
        Game.objects.filter(pk=self.game.pk).update(status=GameStatus.ACTIVE)
        self.as_host()
        before = self.board_question_ids()
        res = self.swap(self.column_b.id, fresh.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("started", res.json()["detail"])
        self.assertEqual(self.board_question_ids(), before)

    def test_swap_unknown_column_409(self):
        fresh = seed_category("Echo", n_questions=3)
        self.as_host()
        res = self.swap(999999, fresh.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("No such column", res.json()["detail"])

    def test_swap_is_host_private(self):
        fresh = seed_category("Foxtrot", n_questions=3)
        self.as_rival()
        res = self.swap(self.column_b.id, fresh.id)
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("SECRET-", json.dumps(res.json()))
        self.client.force_authenticate(None)
        self.assertEqual(self.swap(self.column_b.id, fresh.id).status_code, 401)

    def test_swap_body_validation_400(self):
        self.as_host()
        for bad in ({}, {"category_id": "nope"}, {"category_id": 0}):
            res = self.client.post(
                f"/api/games/{self.game.code}/columns/{self.column_b.id}/replace/", bad, format="json"
            )
            self.assertEqual(res.status_code, 400, bad)

    def test_swap_unknown_code_404(self):
        fresh = seed_category("Golf", n_questions=3)
        self.as_host()
        self.assertEqual(self.swap(self.column_b.id, fresh.id, code="ZZZZ99").status_code, 404)


# ---------------------------------------------------------------------------
# Handoff #10 §F3 — multi-category boards: no duplicates, honest shortages
# ---------------------------------------------------------------------------

class MultiCategoryBoardTests(GameTestBase):
    def overlapping_categories(self, shared, a_extra, b_extra):
        """Two categories sharing `shared` questions, plus per-category extras.
        Uniform difficulty so the draw window never hides pool size."""
        cat_a = Category.objects.create(owner=None, name="Movies A")
        cat_b = Category.objects.create(owner=None, name="Eighties B")

        def make(text, cats):
            q = Question.objects.create(owner=None, question_text=text, answer="A", difficulty=1)
            q.categories.set(cats)
            return q

        for i in range(shared):
            make(f"shared {i}?", [cat_a, cat_b])
        for i in range(a_extra):
            make(f"a-only {i}?", [cat_a])
        for i in range(b_extra):
            make(f"b-only {i}?", [cat_b])
        return cat_a, cat_b

    def test_shared_question_appears_at_most_once_on_the_board(self):
        # A: 3 shared + 2 own = 5; B: 3 shared + 5 own = 8. A 5-per-category
        # build over both needs 10 DISTINCT questions and has exactly 10.
        cat_a, cat_b = self.overlapping_categories(shared=3, a_extra=2, b_extra=5)
        game = create_game(
            host=self.host, mode="points", category_ids=[cat_a.id, cat_b.id], questions_per_category=5
        )
        ids = list(game.cells.values_list("question_id", flat=True))
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10, "a question landed on the board twice")

    def test_overlap_shortage_refuses_instead_of_duplicating(self):
        # A: 5 usable, B: 5 usable — but 3 are the SAME questions, so two
        # 5-question columns can't be filled. The EXCLUDED pool decides; the
        # LATER column reports short (order-dependent attribution, accepted).
        cat_a, cat_b = self.overlapping_categories(shared=3, a_extra=2, b_extra=2)
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="points", category_ids=[cat_a.id, cat_b.id], questions_per_category=5
            )
        detail = str(ctx.exception.detail["categories"])
        self.assertIn("'Eighties B' only has 2 usable question(s); 5 required.", detail)
        self.assertNotIn("Movies A", detail)  # the first column filled fine
        self.assertEqual(Game.objects.count(), 0)  # nothing half-built

    def test_shortage_attribution_follows_column_order(self):
        cat_a, cat_b = self.overlapping_categories(shared=3, a_extra=2, b_extra=2)
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="points", category_ids=[cat_b.id, cat_a.id], questions_per_category=5
            )
        # Same data, reversed order: now A is the later column and reports.
        self.assertIn("Movies A", str(ctx.exception.detail["categories"]))

    def test_deleted_category_reads_as_unknown(self):
        from django.utils import timezone

        cat = seed_category("Soon Gone", n_questions=2)
        cat.deleted_at = timezone.now()
        cat.save(update_fields=["deleted_at"])
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(host=self.host, mode="points", category_ids=[cat.id], questions_per_category=2)
        self.assertIn("Unknown category ids", str(ctx.exception.detail["categories"]))

    def test_replace_still_excludes_the_whole_board(self):
        # Regression guard for §F3's "the replace pool needs no change".
        cat_a, cat_b = self.overlapping_categories(shared=2, a_extra=1, b_extra=6)
        game = create_game(
            host=self.host, mode="points", category_ids=[cat_a.id, cat_b.id], questions_per_category=3
        )
        on_board = set(game.cells.values_list("question_id", flat=True))
        target = game.cells.filter(column__category=cat_b).first()
        from .services import replace_cell_question

        cell = replace_cell_question(code=game.code, cell_id=target.id, host=self.host)
        self.assertNotIn(cell.question_id, on_board)


# ---------------------------------------------------------------------------
# Handoff #10 §H — cells_remaining in the snapshot
# ---------------------------------------------------------------------------

class CellsRemainingTests(GameTestBase):
    """§H1 semantics, pinned: an OPEN cell still counts as remaining; the
    count hits 0 only after the LAST close_cell; reveal/judgment alone never
    move it. Exercised at the services level over a whole tiny 1×2 board —
    exactly what the socket runs."""

    def snapshot(self, game):
        from .serializers import GameStateSerializer

        fresh = (
            Game.objects.prefetch_related("columns__cells", "columns__category", "participants")
            .select_related("current_cell__question", "judged_participant")
            .get(pk=game.pk)
        )
        return GameStateSerializer(fresh).data

    def test_counts_down_only_on_close(self):
        game = make_game(self.host, n=2)
        player = Participant.objects.create(game=game, name="TEAM A")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        self.assertEqual(self.snapshot(game)["cells_remaining"], 2)  # lobby total = per_cat × columns

        cells = list(game.cells.order_by("row"))
        open_cell(code=game.code, cell_id=cells[0].id)
        self.assertEqual(self.snapshot(game)["cells_remaining"], 2)  # OPEN is not answered

        set_buzzer(code=game.code, is_open=True)
        register_buzz(code=game.code, participant=player)
        judge_buzz(code=game.code, participant_id=player.id, correct=True)  # judge + reveal
        self.assertEqual(self.snapshot(game)["cells_remaining"], 2)  # unchanged by reveal/judgment

        close_cell(code=game.code)
        self.assertEqual(self.snapshot(game)["cells_remaining"], 1)

        open_cell(code=game.code, cell_id=cells[1].id)
        close_cell(code=game.code)
        snap = self.snapshot(game)
        self.assertEqual(snap["cells_remaining"], 0)  # exactly after the LAST close
        self.assertIsNone(snap["current_cell"])


# ---------------------------------------------------------------------------
# §F (Handoff #11) — host removes players (soft flag + every active surface)
# ---------------------------------------------------------------------------
class RemovePlayerTests(GameTestBase):
    """services.remove_player + the C6-audited `removed_at__isnull=True`
    filters: snapshot participants/former_players, join cap, token rejoin,
    winners, drink targets, judge lookups, history counts, and the report's
    keep-everything-with-a-flag rule."""

    def setUp(self):
        from .services import remove_player  # noqa: F401 — import check

    def snapshot(self, game):
        from .serializers import GameStateSerializer

        fresh = (
            Game.objects.prefetch_related("columns__cells", "columns__category", "participants")
            .select_related("current_cell__question", "judged_participant", "host")
            .get(pk=game.pk)
        )
        return GameStateSerializer(fresh).data

    def host_seat(self, game):
        return Participant.objects.create(game=game, name="Host", role=ParticipantRole.HOST)

    def test_host_only(self):
        from .services import remove_player

        game = make_game(self.host)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        with self.assertRaisesMessage(ActionError, "Only the host can remove players."):
            remove_player(code=game.code, participant_id=b.id, actor=a)
        b.refresh_from_db()
        self.assertIsNone(b.removed_at)

    def test_host_seat_unremovable(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        with self.assertRaisesMessage(ActionError, "The host seat can't be removed."):
            remove_player(code=game.code, participant_id=seat.id, actor=seat)

    def test_finished_game_refuses(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        (a,) = self.add_players(game, "TEAM A")
        finalize_game(code=game.code)
        with self.assertRaisesMessage(ActionError, "finished"):
            remove_player(code=game.code, participant_id=a.id, actor=seat)

    def test_unknown_participant(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        with self.assertRaisesMessage(ActionError, "Unknown participant."):
            remove_player(code=game.code, participant_id=999999, actor=seat)

    def test_double_remove_exact_structured_shape(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        (a,) = self.add_players(game, "TEAM A")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        with self.assertRaises(StructuredActionError) as ctx:
            remove_player(code=game.code, participant_id=a.id, actor=seat)
        self.assertEqual(set(ctx.exception.payload), {"detail", "code"})  # C4: no extra keys
        self.assertEqual(ctx.exception.payload["code"], "player_removed")

    def test_works_in_lobby_and_active(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        remove_player(code=game.code, participant_id=a.id, actor=seat)  # lobby
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        remove_player(code=game.code, participant_id=b.id, actor=seat)  # active
        a.refresh_from_db(); b.refresh_from_db()
        self.assertIsNotNone(a.removed_at)
        self.assertIsNotNone(b.removed_at)

    # ---- open-cell interactions ------------------------------------------

    def _open_and_buzz(self, game, *players):
        cell = self.cells(game)[0]
        open_cell(code=game.code, cell_id=cell.id)
        set_buzzer(code=game.code, is_open=True)
        for p in players:
            register_buzz(code=game.code, participant=p)
        return cell

    def test_open_cell_buzzes_deleted_historical_kept(self):
        from .models import Buzz
        from .services import remove_player

        game = make_game(self.host, n=2)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        # Round 1: both buzz, B wins with drinks assigned, cell closes —
        # A's buzz on this cell is history and must survive.
        first = self._open_and_buzz(game, a, b)
        judge_buzz(code=game.code, participant_id=b.id, correct=True)
        close_cell(code=game.code)
        # Round 2: both buzz on the fresh cell; A is then removed.
        cells = self.cells(game)
        second = next(c for c in cells if c.id != first.id)
        open_cell(code=game.code, cell_id=second.id)
        set_buzzer(code=game.code, is_open=True)
        register_buzz(code=game.code, participant=a)
        register_buzz(code=game.code, participant=b)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        self.assertFalse(Buzz.objects.filter(cell=second, participant=a).exists())  # live order evaporates
        self.assertTrue(Buzz.objects.filter(cell=second, participant=b).exists())
        self.assertTrue(Buzz.objects.filter(cell=first, participant=a).exists())  # closed-cell history stays

    def test_unjudged_winner_cleared_and_buzzer_reopens(self):
        from .services import remove_player

        game = make_game(self.host, mode="drinks")
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        cell = self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=True)  # locks buzzer, sets marker
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        cell.refresh_from_db()
        game.refresh_from_db()
        self.assertIsNone(cell.answered_by)
        self.assertIsNone(cell.answered_correctly)
        self.assertTrue(game.buzzer_open)  # the round restarts for everyone else
        self.assertIsNone(game.judged_participant)
        self.assertIsNone(game.judged_correct)

    def test_drinks_already_assigned_leaves_the_cell_alone(self):
        from .services import remove_player

        game = make_game(self.host, mode="drinks")
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        cell = self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=True)
        assign_drink(code=game.code, actor=a, target_participant_id=b.id)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        cell.refresh_from_db()
        game.refresh_from_db()
        self.assertEqual(cell.answered_by_id, a.id)  # the drink happened; history stays
        self.assertTrue(cell.answered_correctly)
        self.assertFalse(game.buzzer_open)
        # The judgment marker still clears — the verdict banner would name
        # someone who's gone.
        self.assertIsNone(game.judged_participant)

    def test_judged_wrong_marker_cleared_on_removal(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=False)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        game.refresh_from_db()
        self.assertIsNone(game.judged_participant)
        self.assertIsNone(game.judged_correct)

    def test_removal_keeps_tallies_on_the_row(self):
        from .services import remove_player

        game = make_game(self.host, mode="drinks")
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=True)
        assign_drink(code=game.code, actor=a, target_participant_id=b.id)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        a.refresh_from_db()
        self.assertEqual(a.drinks_given, 1)
        self.assertEqual(a.score, 1)  # history is history

    # ---- seat + name reuse -------------------------------------------------

    def test_name_reusable_after_removal_partial_constraint(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        (a,) = self.add_players(game, "TEAM A")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        again = Participant.objects.create(game=game, name="TEAM A")  # no IntegrityError
        self.assertNotEqual(again.pk, a.pk)
        # And an ACTIVE duplicate still collides.
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Participant.objects.create(game=game, name="TEAM A")

    def test_join_cap_frees_a_seat(self):
        from django.conf import settings
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        names = [f"TEAM {i}" for i in range(settings.MAX_PLAYERS_PER_GAME)]
        players = self.add_players(game, *names)
        url = f"/api/games/{game.code}/join/"
        r = self.client.post(url, {"name": "TEAM LATE"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "game_full")
        remove_player(code=game.code, participant_id=players[0].id, actor=seat)
        r = self.client.post(url, {"name": "TEAM LATE"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_removed_token_cannot_reclaim_falls_through_to_fresh_join(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        (a,) = self.add_players(game, "TEAM A")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        url = f"/api/games/{game.code}/join/"
        r = self.client.post(url, {"name": "TEAM A", "participant_token": a.token}, format="json")
        self.assertEqual(r.status_code, 201)  # a FRESH seat, not a reclaim
        self.assertNotEqual(r.json()["participant"]["id"], a.id)
        self.assertNotEqual(r.json()["participant_token"], a.token)

    def test_active_token_still_reclaims(self):
        game = make_game(self.host)
        (a,) = self.add_players(game, "TEAM A")
        url = f"/api/games/{game.code}/join/"
        r = self.client.post(url, {"name": "TEAM A", "participant_token": a.token}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["participant"]["id"], a.id)

    # ---- snapshot ----------------------------------------------------------

    def snapshot_helper(self, game):
        return RemovePlayerTests.snapshot(self, game)

    def test_snapshot_excludes_removed_and_former_players_carries_attributed_only(self):
        from .services import remove_player

        game = make_game(self.host, mode="drinks", n=2)
        seat = self.host_seat(game)
        a, b, c = self.add_players(game, "TEAM A", "TEAM B", "TEAM C")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        # A wins a cell WITH drinks assigned (attribution sticks), then gets
        # kicked; C is kicked holding nothing.
        cell = self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=True)
        assign_drink(code=game.code, actor=a, target_participant_id=b.id)
        close_cell(code=game.code)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        remove_player(code=game.code, participant_id=c.id, actor=seat)
        snap = self.snapshot(game)
        names = {p["name"] for p in snap["participants"]}
        self.assertNotIn("TEAM A", names)
        self.assertNotIn("TEAM C", names)
        self.assertIn("TEAM B", names)
        self.assertEqual(snap["former_players"], [{"id": a.id, "name": "TEAM A"}])
        # §G's resolution set: the answered cell's id resolves via former_players.
        won = next(
            cl for col in snap["columns"] for cl in col["cells"] if cl["id"] == cell.id
        )
        self.assertEqual(won["answered_by"], a.id)

    def test_snapshot_over_rest_view_excludes_removed(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        r = self.client.get(f"/api/games/{game.code}/")
        self.assertEqual(r.status_code, 200)
        names = {p["name"] for p in r.json()["participants"] if p["role"] == "player"}
        self.assertEqual(names, {"TEAM B"})
        self.assertEqual(r.json()["former_players"], [])

    # ---- finish / report / history ----------------------------------------

    def test_finalize_excludes_removed_from_winners(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Participant.objects.filter(pk=a.pk).update(score=500)
        Participant.objects.filter(pk=b.pk).update(score=100)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        finalize_game(code=game.code)
        game.refresh_from_db()
        self.assertEqual([w.name for w in game.winners.all()], ["TEAM B"])

    def test_report_keeps_removed_with_flag(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        finalize_game(code=game.code)
        self.as_host()
        r = self.client.get(f"/api/games/{game.code}/report/")
        self.assertEqual(r.status_code, 200)
        rows = {p["name"]: p for p in r.json()["participants"] if p["role"] == "player"}
        self.assertEqual(set(rows), {"TEAM A", "TEAM B"})  # ALL participants
        self.assertTrue(rows["TEAM A"]["removed"])
        self.assertFalse(rows["TEAM B"]["removed"])

    def test_history_player_count_excludes_removed(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b, c = self.add_players(game, "TEAM A", "TEAM B", "TEAM C")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        self.as_host()
        r = self.client.get("/api/games/history/")
        row = next(g for g in r.json()["results"] if g["code"] == game.code)
        self.assertEqual(row["participant_count"], 2)

    def test_removed_seat_not_a_drink_target(self):
        from .services import remove_player

        game = make_game(self.host, mode="drinks")
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        self._open_and_buzz(game, a)
        judge_buzz(code=game.code, participant_id=a.id, correct=True)
        remove_player(code=game.code, participant_id=b.id, actor=seat)
        with self.assertRaisesMessage(ActionError, "Pick who drinks."):
            assign_drink(code=game.code, actor=a, target_participant_id=b.id)

    def test_removed_seat_cannot_be_judged(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        Game.objects.filter(pk=game.pk).update(status=GameStatus.ACTIVE)
        self._open_and_buzz(game, a, b)
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        with self.assertRaisesMessage(ActionError, "Unknown participant."):
            judge_buzz(code=game.code, participant_id=a.id, correct=True)

    def test_buzzer_sound_round_robin_counts_active_only(self):
        from .services import remove_player

        game = make_game(self.host)
        seat = self.host_seat(game)
        a, b = self.add_players(game, "TEAM A", "TEAM B")
        remove_player(code=game.code, participant_id=a.id, actor=seat)
        r = self.client.post(f"/api/games/{game.code}/join/", {"name": "TEAM C"}, format="json")
        self.assertEqual(r.status_code, 201)
        # 1 active player (TEAM B) before the join → sound (1 % 4) + 1 == 2.
        self.assertEqual(r.json()["participant"]["buzzer_sound"], 2)


class CellSerializerTripwireTests(GameTestBase):
    """§G (Handoff #11): the answered-cell names are resolved CLIENT-side
    from `answered_by` against participants + former_players — pinned here so
    nobody 'helpfully' adds a name field to CellSerializer later."""

    def test_cell_serializer_exposes_answered_by_and_no_name(self):
        from .serializers import CellSerializer

        game = make_game(self.host)
        (a,) = self.add_players(game, "TEAM A")
        cell = self.cells(game)[0]
        cell.answered_by = a
        cell.answered_correctly = True
        cell.save(update_fields=["answered_by", "answered_correctly"])
        data = CellSerializer(cell).data
        self.assertEqual(
            set(data), {"id", "row", "value", "state", "answered_by", "answered_correctly"}
        )
        self.assertEqual(data["answered_by"], a.id)


# ---------------------------------------------------------------------------
# Handoff #13 §I — tournament mode v1
# ---------------------------------------------------------------------------
class TournamentTestBase(GameTestBase):
    """Shared plumbing: a CREATOR host (tournament creation is plan-gated
    through the quota choke point — free's limit is 0), plus helpers to
    build attached round games and score them."""

    def setUp(self):
        self.host.plan = "creator"
        self.host.save(update_fields=["plan"])
        self.as_host()

    def api_create(self, name="Summer Fest Trivia Tournament", location="Ian's Bar Venue"):
        return self.client.post("/api/tournaments/", {"name": name, "location": location}, format="json")

    def make_tournament(self, owner=None, **kw):
        kw.setdefault("name", f"Cup {Tournament.objects.count()}")
        return Tournament.objects.create(owner=owner or self.host, **kw)

    def attached_game(self, tournament, round_number=1, n=2):
        cat = seed_category(f"TCat-{Game.objects.count()}", n_questions=n)
        return create_game(
            host=self.host, mode="points", category_ids=[cat.id],
            questions_per_category=n, tournament=tournament, round_number=round_number,
        )

    def score_and_finish(self, game, scores: dict):
        """Add players with the given name→score map, then finalize."""
        players = {}
        for name, score in scores.items():
            p = Participant.objects.create(game=game, name=name, score=score)
            players[name] = p
        finalize_game(code=game.code)
        return players


class TournamentCrudTests(TournamentTestBase):
    def test_create_exact_shape_and_list_own_only(self):
        res = self.api_create()
        self.assertEqual(res.status_code, 201, res.data)
        body = res.json()
        self.assertEqual(
            set(body), {"id", "name", "location", "created_at", "finished_at"}
        )
        self.assertEqual(body["name"], "Summer Fest Trivia Tournament")
        self.assertEqual(body["location"], "Ian's Bar Venue")
        self.assertIsNone(body["finished_at"])
        # someone else's tournament never shows in my list
        self.make_tournament(owner=self.rival, name="Rival Cup")
        rows = self.client.get("/api/tournaments/").json()["results"]
        self.assertEqual([r["name"] for r in rows], ["Summer Fest Trivia Tournament"])

    def test_location_optional_and_name_bounds(self):
        res = self.client.post("/api/tournaments/", {"name": "No Venue"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.json()["location"], "")
        self.assertEqual(self.client.post("/api/tournaments/", {}, format="json").status_code, 400)
        res = self.client.post("/api/tournaments/", {"name": "x" * 81}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("name", res.json())

    def test_duplicate_live_name_400_reusable_after_soft_delete(self):
        first = self.api_create().json()
        res = self.api_create()  # same name, same owner, still live
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.json(), {"name": ["You already have a live tournament with that name."]})
        # a DIFFERENT owner may reuse the name (per-owner constraint)
        self.make_tournament(owner=self.rival, name="Summer Fest Trivia Tournament")
        # soft delete frees it for me too (the FOURTH house partial)
        self.assertEqual(self.client.delete(f"/api/tournaments/{first['id']}/").status_code, 204)
        self.assertEqual(self.api_create().status_code, 201)
        row = Tournament.objects.get(pk=first["id"])
        self.assertIsNotNone(row.deleted_at)  # soft, not gone

    def test_retrieve_is_owner_scoped_404(self):
        other = self.make_tournament(owner=self.rival)
        self.assertEqual(self.client.get(f"/api/tournaments/{other.pk}/").status_code, 404)
        deleted = self.make_tournament(deleted_at=timezone.now())
        self.assertEqual(self.client.get(f"/api/tournaments/{deleted.pk}/").status_code, 404)

    def test_finish_is_idempotent(self):
        t = self.make_tournament()
        first = self.client.post(f"/api/tournaments/{t.pk}/finish/").json()
        self.assertIsNotNone(first["finished_at"])
        second = self.client.post(f"/api/tournaments/{t.pk}/finish/")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["finished_at"], first["finished_at"])  # never rewritten

    def test_unauthenticated_401(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/tournaments/").status_code, 401)
        self.assertEqual(self.client.post("/api/tournaments/", {"name": "X"}).status_code, 401)

    def test_list_route_not_swallowed(self):
        # Precedence pin (§I2): the collection root resolves as itself — and
        # the long-standing games/history/ pin lives in HistoryEndpointTests.
        self.assertEqual(self.client.get("/api/tournaments/").status_code, 200)


class TournamentQuotaTests(TournamentTestBase):
    def assert_quota_403(self, res, used, limit):
        self.assertEqual(res.status_code, 403, res.content)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code", "used", "limit"})
        self.assertEqual(body["code"], "quota_tournaments")
        self.assertEqual(body["used"], used)
        self.assertEqual(body["limit"], limit)

    def test_free_user_structured_403(self):
        # The plan gate IS the quota choke point: free's limit is 0.
        self.client.force_authenticate(self.rival)  # plain free account
        res = self.client.post("/api/tournaments/", {"name": "Nope"}, format="json")
        self.assert_quota_403(res, used=0, limit=0)
        # §F3(d) (#19): the upsell copy now sells the lanes that exist
        # (packs/Venue), not the retired "creator account" phrasing.
        self.assertIn("Venue plan", res.json()["detail"])

    def test_expired_creator_plan_collapses_to_free(self):
        self.host.plan_expires_at = timezone.now() - timedelta(days=1)
        self.host.save(update_fields=["plan_expires_at"])
        self.assert_quota_403(self.api_create(), used=0, limit=0)

    def test_override_grants_a_free_user_through_the_choke_point(self):
        self.rival.limit_overrides = {"tournaments": 1}
        self.rival.save(update_fields=["limit_overrides"])
        self.client.force_authenticate(self.rival)
        self.assertEqual(self.client.post("/api/tournaments/", {"name": "Granted"}, format="json").status_code, 201)
        res = self.client.post("/api/tournaments/", {"name": "One too many"}, format="json")
        self.assert_quota_403(res, used=1, limit=1)

    def test_soft_delete_frees_the_slot(self):
        self.host.limit_overrides = {"tournaments": 1}
        self.host.save(update_fields=["limit_overrides"])
        first = self.api_create().json()
        self.assert_quota_403(self.api_create(name="Another"), used=1, limit=1)
        self.client.delete(f"/api/tournaments/{first['id']}/")
        self.assertEqual(self.api_create(name="Another").status_code, 201)

    def test_profile_usage_carries_the_meter(self):
        self.make_tournament()
        usage = self.client.get("/api/auth/profile/").json()["usage"]
        self.assertEqual(usage["tournaments"], {"used": 1, "limit": 25})


class TournamentAttachTests(TournamentTestBase):
    def setUp(self):
        super().setUp()
        self.t = self.make_tournament(name="Attach Cup")
        self.cat = seed_category("AttachCat")

    def api_create_game(self, **extra):
        body = {"mode": "points", "categories": [self.cat.id], "questions_per_category": 2, **extra}
        return self.client.post("/api/games/", body, format="json")

    def test_attach_persists_and_snapshots(self):
        res = self.api_create_game(tournament=self.t.pk, round_number=1)
        self.assertEqual(res.status_code, 201, res.data)
        game = Game.objects.get(code=res.json()["game"]["code"])
        self.assertEqual(game.tournament_id, self.t.pk)
        self.assertEqual(game.round_number, 1)
        self.assertEqual(
            res.json()["game"]["tournament"],
            # location "" falls back server-side (→ brand_name → display
            # name) — the full chain is pinned in SnapshotTournamentTests.
            # §F4b (#17): the block gained `id` (additive amendment — the
            # console's back-to-bracket link needs it).
            {"id": self.t.pk, "name": "Attach Cup", "location": "Host", "round_number": 1},
        )

    def test_created_game_appears_in_detail(self):
        # §F4a (#17): the loop's server truth — the game the create call
        # just made is ALREADY in the bracket payload the frontend reloads
        # into. (The redirect itself is frontend-only; this pins what it
        # relies on: no eventual-consistency window, correct round, lobby
        # status, null standings.)
        res = self.api_create_game(tournament=self.t.pk, round_number=1)
        self.assertEqual(res.status_code, 201, res.data)
        code = res.json()["game"]["code"]
        detail = self.client.get(f"/api/tournaments/{self.t.pk}/").json()
        by_code = {g["code"]: g for g in detail["games"]}
        self.assertIn(code, by_code)
        self.assertEqual(by_code[code]["round_number"], 1)
        self.assertEqual(by_code[code]["status"], "lobby")
        self.assertIsNone(by_code[code]["standings"])

    def test_someone_elses_tournament_404_no_leak(self):
        other = self.make_tournament(owner=self.rival, name="Rival Cup")
        res = self.api_create_game(tournament=other.pk, round_number=1)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"detail": "No such tournament."})
        self.assertEqual(Game.objects.count(), 0)

    def test_deleted_and_unknown_tournament_read_identically(self):
        deleted = self.make_tournament(deleted_at=timezone.now())
        r1 = self.api_create_game(tournament=deleted.pk, round_number=1)
        r2 = self.api_create_game(tournament=999999, round_number=1)
        self.assertEqual((r1.status_code, r2.status_code), (404, 404))
        self.assertEqual(r1.json(), r2.json())

    def test_finished_tournament_409_exact_shape(self):
        self.t.finished_at = timezone.now()
        self.t.save(update_fields=["finished_at"])
        res = self.api_create_game(tournament=self.t.pk, round_number=2)
        self.assertEqual(res.status_code, 409, res.data)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code"})
        self.assertEqual(body["code"], "tournament_finished")
        self.assertEqual(Game.objects.count(), 0)

    def test_pairing_is_both_or_neither(self):
        res = self.api_create_game(tournament=self.t.pk)  # no round_number
        self.assertEqual(res.status_code, 400)
        self.assertIn("round_number", res.json())
        res = self.api_create_game(round_number=2)  # no tournament
        self.assertEqual(res.status_code, 400)
        self.assertIn("round_number", res.json())
        with self.assertRaises(DRFValidationError):  # the service is the gate too
            create_game(host=self.host, mode="points", category_ids=[self.cat.id],
                        questions_per_category=2, tournament=self.t)

    def test_round_zero_rejected(self):
        res = self.api_create_game(tournament=self.t.pk, round_number=0)
        self.assertEqual(res.status_code, 400)
        self.assertIn("round_number", res.json())


class TournamentAdvanceTests(TournamentTestBase):
    def setUp(self):
        super().setUp()
        self.t = self.make_tournament(name="Advance Cup")

    def advance(self, round_number=1, per_game=1, tournament=None):
        t = tournament or self.t
        return self.client.post(
            f"/api/tournaments/{t.pk}/rounds/{round_number}/advance/",
            {"per_game": per_game}, format="json",
        )

    def test_game_standings_competition_ranking(self):
        game = self.attached_game(self.t)
        self.score_and_finish(game, {"A": 7, "B": 7, "C": 2})
        game = Game.objects.prefetch_related("participants").get(pk=game.pk)
        self.assertEqual(
            game_standings(game),
            [{"name": "A", "score": 7, "rank": 1},
             {"name": "B", "score": 7, "rank": 1},
             {"name": "C", "score": 2, "rank": 3}],
        )

    def test_unfinished_round_409_exact_shape(self):
        self.attached_game(self.t)  # never finished
        res = self.advance()
        self.assertEqual(res.status_code, 409, res.data)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code"})
        self.assertEqual(body["code"], "tournament_round_incomplete")
        self.assertEqual(TournamentAdvancer.objects.count(), 0)

    def test_empty_round_409_exact_shape(self):
        res = self.advance(round_number=3)
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code"})
        self.assertEqual(body["code"], "tournament_round_empty")

    def test_top_1_across_two_games_ties_included(self):
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        g2 = self.attached_game(self.t)
        self.score_and_finish(g2, {"C": 7, "D": 7, "E": 2})  # tie at the top
        res = self.advance(per_game=1)
        self.assertEqual(res.status_code, 200, res.data)
        body = res.json()
        self.assertEqual(body["round_number"], 1)
        self.assertEqual(body["per_game"], 1)
        rows = body["advancers"]
        # §F2 (#20): rows gained `id` + `target_game` ADDITIVELY — this
        # exact assertion moved in the same session (the #17 precedent).
        # No round-2 game exists here, so every target is null; ids are
        # asserted by type (autoincrement) and popped for the exact match.
        for row in rows:
            self.assertIsInstance(row.pop("id"), int)
        self.assertEqual(
            rows,
            [{"round_number": 1, "name": "A", "rank": 1, "source_game": g1.code, "target_game": None},
             {"round_number": 1, "name": "C", "rank": 1, "source_game": g2.code, "target_game": None},
             {"round_number": 1, "name": "D", "rank": 1, "source_game": g2.code, "target_game": None}],
        )

    def test_top_2_uses_competition_ranks(self):
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3, "C": 1})
        g2 = self.attached_game(self.t)
        self.score_and_finish(g2, {"D": 7, "E": 7, "F": 2})  # F is rank 3
        res = self.advance(per_game=2)
        names = [(r["name"], r["rank"]) for r in res.json()["advancers"]]
        self.assertEqual(names, [("A", 1), ("B", 2), ("D", 1), ("E", 1)])  # F stays out

    def test_rerun_replaces_the_rounds_rows(self):
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        self.advance(per_game=1)
        self.assertEqual(TournamentAdvancer.objects.count(), 1)
        res = self.advance(per_game=2)  # the host changed their mind
        self.assertEqual(res.status_code, 200)
        rows = TournamentAdvancer.objects.filter(tournament=self.t, round_number=1)
        self.assertEqual({(r.name, r.rank) for r in rows}, {("A", 1), ("B", 2)})
        self.assertEqual(rows.count(), 2)  # replaced, not appended

    def test_rounds_are_independent(self):
        g1 = self.attached_game(self.t, round_number=1)
        self.score_and_finish(g1, {"A": 5})
        self.advance(round_number=1, per_game=1)
        g2 = self.attached_game(self.t, round_number=2)
        self.score_and_finish(g2, {"A": 9})
        self.advance(round_number=2, per_game=1)
        by_round = {r.round_number for r in TournamentAdvancer.objects.filter(tournament=self.t)}
        self.assertEqual(by_round, {1, 2})
        # re-running round 2 leaves round 1 untouched
        self.advance(round_number=2, per_game=1)
        self.assertEqual(TournamentAdvancer.objects.filter(round_number=1).count(), 1)

    def test_removed_players_never_advance(self):
        game = self.attached_game(self.t)
        players = self.score_and_finish(game, {"A": 9, "B": 3})
        players["A"].removed_at = timezone.now()
        players["A"].save(update_fields=["removed_at"])
        res = self.advance(per_game=1)
        self.assertEqual([r["name"] for r in res.json()["advancers"]], ["B"])

    def test_per_game_strictly_1_or_2(self):
        game = self.attached_game(self.t)
        self.score_and_finish(game, {"A": 1})
        for bad in (0, 3, True, "1", None):
            res = self.client.post(
                f"/api/tournaments/{self.t.pk}/rounds/1/advance/", {"per_game": bad}, format="json"
            )
            self.assertEqual(res.status_code, 400, (bad, res.data))
            self.assertIn("per_game", res.json())
        # omitted → defaults to 1
        res = self.client.post(f"/api/tournaments/{self.t.pk}/rounds/1/advance/", {}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["per_game"], 1)

    def test_finished_tournament_rejects_advance(self):
        game = self.attached_game(self.t)
        self.score_and_finish(game, {"A": 1})
        self.t.finished_at = timezone.now()
        self.t.save(update_fields=["finished_at"])
        res = self.advance()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "tournament_finished")

    def test_advance_is_owner_scoped(self):
        other = self.make_tournament(owner=self.rival, name="Rival Cup")
        self.assertEqual(self.advance(tournament=other).status_code, 404)

    def test_service_is_atomic_on_rejection(self):
        # An unfinished game ANYWHERE in the round leaves prior rows intact:
        # the delete+rewrite happens inside one transaction that never commits.
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5})
        self.advance(per_game=1)
        self.attached_game(self.t)  # a second, unfinished round-1 game
        res = self.advance(per_game=1)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(TournamentAdvancer.objects.filter(round_number=1).count(), 1)  # round 1's rows survived


class TournamentDetailAndRule5Tests(TournamentTestBase):
    def test_detail_games_standings_and_advancers(self):
        t = self.make_tournament(name="Detail Cup", location="The Snug")
        g1 = self.attached_game(t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        live = self.attached_game(t, round_number=2)  # unfinished → null standings
        advance_round(tournament=t, round_number=1, per_game=1)
        body = self.client.get(f"/api/tournaments/{t.pk}/").json()
        self.assertEqual(
            set(body), {"id", "name", "location", "created_at", "finished_at", "games", "advancers"}
        )
        by_code = {g["code"]: g for g in body["games"]}
        self.assertEqual(
            set(by_code[g1.code]),
            {"code", "mode", "status", "round_number", "created_at", "finished_at", "standings"},
        )
        self.assertEqual(
            by_code[g1.code]["standings"],
            [{"name": "A", "score": 5, "rank": 1}, {"name": "B", "score": 3, "rank": 2}],
        )
        self.assertIsNone(by_code[live.code]["standings"])
        # §F2 (#20): advancer rows gained id/target_game/claimed ADDITIVELY —
        # assertion moved in the same session. `live` is the ONLY round-2
        # game when Advance runs, so §F1b's auto-target fires (advance-time
        # direction) and shows up right here; nothing is claimed yet.
        (adv_row,) = body["advancers"]
        self.assertIsInstance(adv_row.pop("id"), int)
        self.assertEqual(
            adv_row,
            {"round_number": 1, "name": "A", "rank": 1, "source_game": g1.code,
             "target_game": live.code, "claimed": False},
        )

    def test_no_question_content_anywhere_in_tournament_payloads(self):
        # Rule 5, the grep way (#12's public-categories pattern): play a full
        # cell — question opened, answer revealed, cell closed — then assert
        # the seeded SECRET- answers and any question text appear NOWHERE in
        # the detail or advance payloads.
        t = self.make_tournament(name="Secret Cup")
        game = self.attached_game(t)
        (player,) = self.add_players(game, "A")
        cell = self.cells(game)[0]
        open_cell(code=game.code, cell_id=cell.id)
        set_buzzer(code=game.code, is_open=True)
        register_buzz(code=game.code, participant=player)
        judge_buzz(code=game.code, participant_id=player.pk, correct=True)
        close_cell(code=game.code)
        finalize_game(code=game.code)
        detail = self.client.get(f"/api/tournaments/{t.pk}/").json()
        adv = self.client.post(
            f"/api/tournaments/{t.pk}/rounds/1/advance/", {"per_game": 1}, format="json"
        ).json()
        for payload in (detail, adv):
            blob = json.dumps(payload)
            self.assertNotIn("SECRET-", blob)
            self.assertNotIn("question_text", blob)
            self.assertNotIn("answer", blob)


class SnapshotTournamentTests(TournamentTestBase):
    def test_plain_game_snapshot_tournament_null(self):
        # The forward-path shape: every pre-#13 game (and every plain new
        # one) carries tournament: null and behaves exactly as before.
        game = make_game(self.host)
        snap = self.client.get(f"/api/games/{game.code}/").json()
        self.assertIsNone(snap["tournament"])

    def test_attached_game_snapshot_block_exact_shape(self):
        t = self.make_tournament(name="Snap Cup", location="Ian's Bar Venue")
        game = self.attached_game(t, round_number=2)
        snap = self.client.get(f"/api/games/{game.code}/").json()
        # §F4b (#17): `id` joined the pinned block (additive amendment).
        self.assertEqual(
            snap["tournament"],
            {"id": t.pk, "name": "Snap Cup", "location": "Ian's Bar Venue", "round_number": 2},
        )

    def test_location_falls_back_to_brand_then_display_name(self):
        t = self.make_tournament(name="Fallback Cup")  # location ""
        game = self.attached_game(t)
        code = game.code
        self.host.brand_name = "THE KINGS ARMS"
        self.host.save(update_fields=["brand_name"])
        snap = self.client.get(f"/api/games/{code}/").json()
        self.assertEqual(snap["tournament"]["location"], "THE KINGS ARMS")
        self.host.brand_name = ""
        self.host.save(update_fields=["brand_name"])
        snap = self.client.get(f"/api/games/{code}/").json()
        self.assertEqual(snap["tournament"]["location"], "Host")  # display_name
        self.host.display_name = ""
        self.host.save(update_fields=["display_name"])
        snap = self.client.get(f"/api/games/{code}/").json()
        self.assertIsNone(snap["tournament"]["location"])  # frontend hides the line

    def test_join_response_snapshot_carries_it(self):
        t = self.make_tournament(name="Join Cup", location="The Snug")
        game = self.attached_game(t)
        res = self.client.post(f"/api/games/{game.code}/join/", {"name": "Team A"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["game"]["tournament"]["name"], "Join Cup")
        # §F4b (#17): the id rides the player-facing surface too — pinned
        # where it's born; its inertness is the test below.
        self.assertEqual(res.json()["game"]["tournament"]["id"], t.pk)

    def test_snapshot_tournament_id_is_inert_without_owner_token(self):
        # §F4b (#17): the safety claim behind shipping the id to player
        # surfaces, pinned — the id is meaningless without the OWNER's Knox
        # token. Anonymous → 401; a different signed-in host → the same 404
        # a nonexistent id gets (no existence leak).
        t = self.make_tournament(name="Inert Cup")
        game = self.attached_game(t)
        snap_id = self.client.get(f"/api/games/{game.code}/").json()["tournament"]["id"]
        self.assertEqual(snap_id, t.pk)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/api/tournaments/{snap_id}/").status_code, 401)
        self.as_rival()
        self.assertEqual(self.client.get(f"/api/tournaments/{snap_id}/").status_code, 404)


# --- Handoff #20: §F1 advancer↔participant linkage + §F3 seat claims --------


class AdvancerLinkageTests(TournamentTestBase):
    """§F1d: source_participant on every row, C-1's inclusive cut PINNED
    (advance_round was already `rank <= per_game` — this stops a refactor
    from unpinning it), re-run correctness, the §F1c claim-ledger
    constraint, and C-4's auto-target from BOTH directions."""

    def setUp(self):
        super().setUp()
        self.t = self.make_tournament(name="Linkage Cup")

    def test_advancers_carry_source_participant(self):
        game = self.attached_game(self.t)
        players = self.score_and_finish(game, {"A": 5, "B": 3})
        rows = advance_round(tournament=self.t, round_number=1, per_game=2)
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["A"].source_participant_id, players["A"].pk)
        self.assertEqual(by_name["B"].source_participant_id, players["B"].pk)

    def test_tie_at_the_qualifying_cut_advances_everyone_in_it(self):
        # C-1: two teams share rank 2 under per_game=2 → BOTH advance
        # (three rows total from this game).
        game = self.attached_game(self.t)
        self.score_and_finish(game, {"A": 9, "B": 4, "C": 4, "D": 1})
        rows = advance_round(tournament=self.t, round_number=1, per_game=2)
        self.assertEqual({(r.name, r.rank) for r in rows}, {("A", 1), ("B", 2), ("C", 2)})

    def test_rerun_replace_keeps_source_participant_correct(self):
        game = self.attached_game(self.t)
        players = self.score_and_finish(game, {"A": 5, "B": 3})
        advance_round(tournament=self.t, round_number=1, per_game=1)
        rows = advance_round(tournament=self.t, round_number=1, per_game=2)
        self.assertEqual(TournamentAdvancer.objects.filter(tournament=self.t).count(), 2)
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["A"].source_participant_id, players["A"].pk)
        self.assertEqual(by_name["B"].source_participant_id, players["B"].pk)

    def test_claim_ledger_constraint_holds(self):
        # §F1c: one claim per (target game, source seat) — the DB says no
        # to a duplicate even if every endpoint check were bypassed.
        from django.db import IntegrityError, transaction as _tx

        g1 = self.attached_game(self.t)
        players = self.score_and_finish(g1, {"A": 5})
        g2 = self.attached_game(self.t, round_number=2)
        Participant.objects.create(game=g2, name="A", claimed_from=players["A"])
        with self.assertRaises(IntegrityError):
            with _tx.atomic():
                Participant.objects.create(game=g2, name="A COPY", claimed_from=players["A"])
        # …but the SAME source claiming into a DIFFERENT game is allowed
        # (the host-retarget lane the (game, claimed_from) pair exists for).
        g3 = self.attached_game(self.t, round_number=2)
        Participant.objects.create(game=g3, name="A", claimed_from=players["A"])

    def test_auto_target_fires_at_advance_time(self):
        # Direction 1: the round-2 game already exists when Advance runs.
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        g2 = self.attached_game(self.t, round_number=2)
        rows = advance_round(tournament=self.t, round_number=1, per_game=2)
        self.assertEqual({r.target_game_id for r in rows}, {g2.pk})

    def test_auto_target_fires_when_next_round_game_is_created_later(self):
        # Direction 2 (the common flow): Advance first, build the board after.
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        advance_round(tournament=self.t, round_number=1, per_game=2)
        self.assertEqual(
            set(
                TournamentAdvancer.objects.filter(tournament=self.t).values_list(
                    "target_game", flat=True
                )
            ),
            {None},
        )
        g2 = self.attached_game(self.t, round_number=2)
        self.assertEqual(
            set(
                TournamentAdvancer.objects.filter(tournament=self.t).values_list(
                    "target_game", flat=True
                )
            ),
            {g2.pk},
        )

    def test_second_next_round_game_clears_auto_targets(self):
        # C-4a (flagged ruling): the moment a round splits into TWO games,
        # single-game auto-targets stop being meaningful — C-4 says
        # multi-game rounds are host-assigned, so targets clear and the
        # console's selector takes over. ONLY the 1→2 transition clears:
        # a third board added to an already-host-managed round leaves the
        # host's manual routing alone (pinned below).
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        g2 = self.attached_game(self.t, round_number=2)
        advance_round(tournament=self.t, round_number=1, per_game=2)
        g2b = self.attached_game(self.t, round_number=2)  # the split
        targets = lambda: set(  # noqa: E731 — tiny local reader
            TournamentAdvancer.objects.filter(tournament=self.t).values_list(
                "target_game", flat=True
            )
        )
        self.assertEqual(targets(), {None})
        self.assertTrue(Game.objects.filter(pk=g2.pk).exists())  # nothing else touched
        # Host routes both qualifiers by hand; a THIRD board leaves it alone.
        TournamentAdvancer.objects.filter(tournament=self.t, name="A").update(target_game=g2)
        TournamentAdvancer.objects.filter(tournament=self.t, name="B").update(target_game=g2b)
        self.attached_game(self.t, round_number=2)
        self.assertEqual(targets(), {g2.pk, g2b.pk})

    def test_rerun_preserves_host_routing_in_multi_game_rounds(self):
        # "Top-1 → top-2" must not force re-routing the winner: rows the
        # re-run rebuilds keep their (source_game, name)-matched target;
        # only genuinely NEW qualifiers arrive unrouted.
        g1 = self.attached_game(self.t)
        self.score_and_finish(g1, {"A": 5, "B": 3})
        g2 = self.attached_game(self.t, round_number=2)
        self.attached_game(self.t, round_number=2)  # two targets → host-assigned lane
        advance_round(tournament=self.t, round_number=1, per_game=1)
        TournamentAdvancer.objects.filter(tournament=self.t, name="A").update(target_game=g2)
        rows = advance_round(tournament=self.t, round_number=1, per_game=2)
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["A"].target_game_id, g2.pk)  # preserved
        self.assertIsNone(by_name["B"].target_game_id)  # new qualifier, unrouted


class ClaimEndpointTests(TournamentTestBase):
    """§F3d: the full checklist — happy path end-to-end, then every
    rejection with its structured code, plus the my_advancement block and
    the host's target-assignment endpoint."""

    def setUp(self):
        super().setUp()
        self.t = self.make_tournament(name="Claim Cup")
        self.g1 = self.attached_game(self.t)
        self.players = self.score_and_finish(self.g1, {"WINNERS": 9, "ALSO": 5, "LOSERS": 1})
        self.g2 = self.attached_game(self.t, round_number=2)
        advance_round(tournament=self.t, round_number=1, per_game=2)  # auto-targets g2

    def claim(self, code=None, token=None):
        return self.client.post(
            f"/api/games/{(code or self.g2.code)}/claim/",
            {"participant_token": token if token is not None else self.players["WINNERS"].token},
            format="json",
        )

    def test_happy_path_finish_advance_claim_seated(self):
        res = self.claim()
        self.assertEqual(res.status_code, 201, res.data)
        body = res.json()
        # EXACTLY the join response contract: participant + token + snapshot.
        self.assertEqual(set(body), {"participant", "participant_token", "game"})
        seat = Participant.objects.get(game=self.g2, name="WINNERS")
        self.assertEqual(seat.role, ParticipantRole.PLAYER)
        self.assertEqual(seat.claimed_from_id, self.players["WINNERS"].pk)
        self.assertEqual(body["participant"]["id"], seat.pk)
        self.assertEqual(body["participant_token"], seat.token)
        self.assertEqual(body["game"]["code"], self.g2.code)
        # The new token is a REAL seat token: it rejoins like any other.
        rejoin = self.client.post(
            f"/api/games/{self.g2.code}/join/",
            {"name": "WINNERS", "participant_token": seat.token},
            format="json",
        )
        self.assertEqual(rejoin.status_code, 200)

    def test_double_claim_409(self):
        self.assertEqual(self.claim().status_code, 201)
        res = self.claim()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "claim_already_claimed")

    def test_losers_token_403(self):
        res = self.claim(token=self.players["LOSERS"].token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "claim_not_qualified")

    def test_wrong_target_403(self):
        other = self.attached_game(self.make_tournament(name="Other Cup"))
        res = self.claim(code=other.code)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "claim_not_qualified")

    def test_forged_and_removed_tokens_401(self):
        self.assertEqual(self.claim(token="forged-token").status_code, 401)
        winner = self.players["WINNERS"]
        winner.removed_at = timezone.now()
        winner.save(update_fields=["removed_at"])
        self.assertEqual(self.claim().status_code, 401)  # a removed seat's token is dead

    def test_target_started_409(self):
        self.g2.status = GameStatus.ACTIVE
        self.g2.save(update_fields=["status"])
        res = self.claim()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "claim_target_started")

    def test_source_unfinished_409(self):
        # Only reachable with a hand-crafted advancer (advance requires a
        # finished round) — pinned anyway: the check order is the contract.
        fresh = self.attached_game(self.t)  # round 1, lobby
        seat = Participant.objects.create(game=fresh, name="EARLY")
        TournamentAdvancer.objects.create(
            tournament=self.t, round_number=1, name="EARLY", source_game=fresh,
            rank=1, source_participant=seat, target_game=self.g2,
        )
        res = self.claim(token=seat.token)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "claim_source_unfinished")

    def test_name_conflict_exact_shape(self):
        # C-8: a walk-up already took the name → structured, NAMED, no
        # silent suffixing; the host resolves and the team re-taps.
        self.client.post(f"/api/games/{self.g2.code}/join/", {"name": "WINNERS"}, format="json")
        res = self.claim()
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code", "name"})
        self.assertEqual(body["code"], "claim_name_taken")
        self.assertEqual(body["name"], "WINNERS")
        # host kicks the walk-up → the claim goes through
        walkup = Participant.objects.get(game=self.g2, name="WINNERS")
        walkup.removed_at = timezone.now()
        walkup.save(update_fields=["removed_at"])
        self.assertEqual(self.claim().status_code, 201)

    def test_full_target_gets_the_pinned_game_full_shape(self):
        # Flagged ruling: the cap holds for claims too, with the EXACT
        # game_full contract the join endpoint pins.
        for i in range(6):
            self.client.post(f"/api/games/{self.g2.code}/join/", {"name": f"WALKUP {i}"}, format="json")
        res = self.claim()
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(set(body), {"detail", "code", "limit"})
        self.assertEqual(body["code"], "game_full")

    def test_unassigned_advancer_cannot_claim(self):
        # C-4: no target (round split, host hasn't routed yet) → the phone
        # shows "ask your host"; a direct POST is claim_not_qualified.
        TournamentAdvancer.objects.filter(tournament=self.t).update(target_game=None)
        res = self.claim()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "claim_not_qualified")

    def test_my_advancement_block_and_privacy(self):
        code = self.g1.code
        winner_token = self.players["WINNERS"].token
        # No seat param → null (the WS-broadcast equivalence).
        self.assertIsNone(self.client.get(f"/api/games/{code}/").json()["my_advancement"])
        # Loser's token → null (indistinguishable from not-yet-advanced).
        loser = self.client.get(f"/api/games/{code}/?seat={self.players['LOSERS'].token}").json()
        self.assertIsNone(loser["my_advancement"])
        # Winner's token → the pinned block; rank + target + claimed only —
        # never ids, never tokens (§B rule 5).
        snap = self.client.get(f"/api/games/{code}/?seat={winner_token}").json()
        block = snap["my_advancement"]
        self.assertEqual(set(block), {"rank", "target", "claimed"})
        self.assertEqual(block["rank"], 1)
        self.assertFalse(block["claimed"])
        self.assertEqual(
            block["target"], {"code": self.g2.code, "status": "lobby", "round_number": 2}
        )
        self.assertNotIn(winner_token, json.dumps(snap["my_advancement"]))
        # After the claim, the same poll flips claimed.
        self.claim()
        block = self.client.get(f"/api/games/{code}/?seat={winner_token}").json()["my_advancement"]
        self.assertTrue(block["claimed"])
        # A bogus seat token → null, not an error (display-only sugar).
        self.assertIsNone(
            self.client.get(f"/api/games/{code}/?seat=nonsense").json()["my_advancement"]
        )
        # Unfinished tournament game → null even for a real seat.
        g3 = self.attached_game(self.t, round_number=2)
        seat = Participant.objects.create(game=g3, name="EAGER")
        self.assertIsNone(
            self.client.get(f"/api/games/{g3.code}/?seat={seat.token}").json()["my_advancement"]
        )

    def test_target_assignment_endpoint(self):
        advancer = TournamentAdvancer.objects.filter(tournament=self.t, name="WINNERS").get()
        url = f"/api/tournaments/{self.t.pk}/advancers/{advancer.pk}/target/"
        g2b = self.attached_game(self.t, round_number=2)  # split → targets cleared (C-4a)
        advancer.refresh_from_db()
        self.assertIsNone(advancer.target_game_id)
        # Assign to the second round-2 game.
        res = self.client.post(url, {"game": g2b.code.lower()}, format="json")  # case-insensitive
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.json()["target_game"], g2b.code)
        # Wrong round → 400; foreign game → 400.
        res = self.client.post(url, {"game": self.g1.code}, format="json")
        self.assertEqual(res.status_code, 400)
        foreign = make_game(self.rival)
        res = self.client.post(url, {"game": foreign.code}, format="json")
        self.assertEqual(res.status_code, 400)
        # Clear with null.
        res = self.client.post(url, {"game": None}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["target_game"])
        # Owner-scoped 404 + the pinned finished 409.
        self.as_rival()
        self.assertEqual(self.client.post(url, {"game": g2b.code}, format="json").status_code, 404)
        self.as_host()
        self.t.finished_at = timezone.now()
        self.t.save(update_fields=["finished_at"])
        res = self.client.post(url, {"game": g2b.code}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "tournament_finished")


# --- Handoff #18: §F5 hand-picked boards + §F4/§F7/§F8 hosting gates --------


from datetime import timedelta as _td18

from billing.models import Entitlement as _Ent18, EntitlementKind as _Kind18, Purchase as _P18


def _pack(user, kind=None, *, question_limit=50, game_limit=None, active=True, session="cs_hp"):
    """A hand-made paid entitlement (the webhook's product, minus Stripe)."""
    purchase = _P18.objects.create(
        user=user, product_key="party_game_50", stripe_checkout_session_id=session
    )
    until = timezone.now() + (_td18(days=30) if active else -_td18(days=1))
    return _Ent18.objects.create(
        user=user,
        kind=kind or _Kind18.PARTY_PACK,
        source_purchase=purchase,
        question_limit=question_limit,
        game_limit=game_limit,
        active_from=timezone.now() - _td18(days=31 if not active else 0),
        active_until=until,
    )


class HandPickedBoardTests(APITestCase):
    """§F5: validation, ordering ruling, gate, mixed boards."""

    def setUp(self):
        self.host = User.objects.create_user("hp@test.com", "sturdy-pass-123", plan="creator")
        self.cat = seed_category("HandPick", 8)
        self.other = seed_category("Drawn", 5)
        self.client.force_authenticate(self.host)

    def _qids(self, cat, n=None):
        ids = list(
            cat.questions.order_by("id").values_list("id", flat=True)
        )
        return ids if n is None else ids[:n]

    def test_order_preserved_and_values_scale_by_row(self):
        # Pick 3 in a deliberately non-difficulty order: hardest first.
        ids = self._qids(self.cat)
        chosen = [ids[7], ids[0], ids[3]]
        game = create_game(
            host=self.host,
            mode="points",
            category_ids=[self.cat.id],
            questions_per_category=3,
            hand_picked={str(self.cat.id): chosen},
        )
        cells = list(game.cells.order_by("row"))
        self.assertEqual([c.question_id for c in cells], chosen)  # the host's climb
        self.assertEqual([c.value for c in cells], [100, 200, 300])  # scaling unchanged

    def test_mixed_picked_and_drawn_board_no_duplicates(self):
        ids = self._qids(self.cat, 5)
        game = create_game(
            host=self.host,
            mode="drinks",
            category_ids=[self.cat.id, self.other.id],
            questions_per_category=5,
            hand_picked={self.cat.id: ids},
        )
        self.assertEqual(game.cells.count(), 10)
        picked_col = game.columns.get(category=self.cat)
        self.assertEqual(
            [c.question_id for c in picked_col.cells.order_by("row")], ids
        )
        all_qids = list(game.cells.values_list("question_id", flat=True))
        self.assertEqual(len(all_qids), len(set(all_qids)))

    def test_length_dup_ownership_archived_offenders_listed(self):
        ids = self._qids(self.cat)
        # wrong length
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="drinks", category_ids=[self.cat.id],
                questions_per_category=3, hand_picked={self.cat.id: ids[:2]},
            )
        self.assertIn("exactly 3", str(ctx.exception))
        # duplicate within the column
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="drinks", category_ids=[self.cat.id],
                questions_per_category=3, hand_picked={self.cat.id: [ids[0], ids[0], ids[1]]},
            )
        self.assertIn("repeated", str(ctx.exception))
        # foreign question id (belongs to the OTHER category)
        foreign = self._qids(self.other, 1)[0]
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="drinks", category_ids=[self.cat.id],
                questions_per_category=3, hand_picked={self.cat.id: [ids[0], ids[1], foreign]},
            )
        self.assertIn(str(foreign), str(ctx.exception))
        # archived question is not usable
        archived = Question.objects.get(pk=ids[2])
        archived.is_archived = True
        archived.save(update_fields=["is_archived"])
        with self.assertRaises(DRFValidationError) as ctx:
            create_game(
                host=self.host, mode="drinks", category_ids=[self.cat.id],
                questions_per_category=3, hand_picked={self.cat.id: [ids[0], ids[1], ids[2]]},
            )
        self.assertIn(str(ids[2]), str(ctx.exception))
        # unknown hand_picked key (category not on the board)
        with self.assertRaises(DRFValidationError):
            create_game(
                host=self.host, mode="drinks", category_ids=[self.cat.id],
                questions_per_category=3, hand_picked={self.other.id: ids[:3]},
            )

    def test_gate_free_user_403_paid_or_entitled_pass(self):
        free = User.objects.create_user("free-hp@test.com", "sturdy-pass-123")
        self.client.force_authenticate(free)
        ids = self._qids(self.cat, 3)
        body = {
            "mode": "drinks", "categories": [self.cat.id], "questions_per_category": 3,
            "hand_picked": {str(self.cat.id): ids},
        }
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "hand_pick_locked")
        # …but a pack buyer (active entitlement, NOT plan creator) may.
        _pack(free)
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        col = Game.objects.get(code=r.json()["game"]["code"]).columns.first()
        self.assertEqual([c.question_id for c in col.cells.order_by("row")], ids)
        # And the plain create without hand_picked never trips the gate.
        self.client.force_authenticate(User.objects.create_user("plain@test.com", "x-pass-123"))
        r = self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [self.cat.id], "questions_per_category": 3},
            format="json",
        )
        self.assertEqual(r.status_code, 201)


class HostingGateTests(APITestCase):
    """§F4 pack hosting + §F7 plan_required: exact 403 shapes."""

    def setUp(self):
        self.buyer = User.objects.create_user("gates@test.com", "sturdy-pass-123")
        self.client.force_authenticate(self.buyer)
        self.library = seed_category("FreeLibrary", 5)

    def _bound_category(self, entitlement, n=3):
        cat = Category.objects.create(owner=self.buyer, name=f"Pack {entitlement.pk}", entitlement=entitlement)
        for i in range(n):
            q = Question.objects.create(
                owner=self.buyer, question_text=f"pk{entitlement.pk} q{i}?", answer=f"a{i}",
                difficulty=1,
            )
            q.categories.add(cat)
        return cat

    def test_active_pack_hosts_expired_pack_403_pack_inactive(self):
        ent = _pack(self.buyer)
        cat = self._bound_category(ent)
        body = {"mode": "drinks", "categories": [cat.id], "questions_per_category": 3}
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        ent.active_until = timezone.now() - timedelta(minutes=1)
        ent.save(update_fields=["active_until"])
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(set(r.json()), {"detail", "code"})  # exact shape
        self.assertEqual(r.json()["code"], "pack_inactive")
        self.assertIn("Reactivate", r.json()["detail"])  # upsell names reactivation

    def test_expired_buyer_keeps_free_library_hosting(self):
        ent = _pack(self.buyer, active=False)
        self._bound_category(ent)
        r = self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [self.library.id], "questions_per_category": 3},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_own_unbound_needs_authoring_rights(self):
        # A free user who somehow owns unbound content (lapsed plan) is
        # gated with plan_required; a venue-active or creator host isn't.
        own = Category.objects.create(owner=self.buyer, name="My old stuff")
        for i in range(3):
            q = Question.objects.create(
                owner=self.buyer, question_text=f"own {i}?", answer="x", difficulty=1
            )
            q.categories.add(own)
        body = {"mode": "drinks", "categories": [own.id], "questions_per_category": 3}
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(set(r.json()), {"detail", "code"})
        self.assertEqual(r.json()["code"], "plan_required")
        self.buyer.plan = "creator"
        self.buyer.save(update_fields=["plan"])
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_column_swap_mirrors_the_gates(self):
        ent = _pack(self.buyer)
        bound = self._bound_category(ent)
        r = self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [self.library.id], "questions_per_category": 3},
            format="json",
        )
        code = r.json()["game"]["code"]
        column_id = r.json()["game"]["columns"][0]["id"]
        ent.active_until = timezone.now() - timedelta(minutes=1)
        ent.save(update_fields=["active_until"])
        r = self.client.post(
            f"/api/games/{code}/columns/{column_id}/replace/",
            {"category_id": bound.id},
            format="json",
        )
        self.assertEqual(r.status_code, 409)
        self.assertIn("pack window has ended", r.json()["detail"])


class TournamentPassTests(APITestCase):
    """§F8 slice: consumption + attach limits."""

    def setUp(self):
        self.buyer = User.objects.create_user("pass@test.com", "sturdy-pass-123")
        self.client.force_authenticate(self.buyer)
        self.cat = seed_category("PassCat", 5)

    def _pass(self, session="cs_pass1", active=True):
        return _pack(
            self.buyer, kind=_Kind18.TOURNAMENT_PASS,
            question_limit=200, game_limit=2, active=active, session=session,
        )

    def test_pass_permits_one_create_and_is_consumed(self):
        r = self.client.post("/api/tournaments/", {"name": "Cup", "location": ""}, format="json")
        self.assertEqual(r.status_code, 403)  # free: quota_tournaments
        ent = self._pass()
        r = self.client.post("/api/tournaments/", {"name": "Cup", "location": ""}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        tid = r.json()["id"]
        ent.refresh_from_db()
        self.assertEqual(ent.tournament_id, tid)  # consumed
        # Second create: the union quota itself denies (1 active pass = limit
        # 1, 1 live tournament = used 1) — accurate numbers, generic message.
        r = self.client.post("/api/tournaments/", {"name": "Cup 2", "location": ""}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "quota_tournaments")
        self.assertEqual((r.json()["used"], r.json()["limit"]), (1, 1))
        # The SPENT-PASS corner: soft-deleting the tournament frees the used
        # count, but the pass stays bound (soft delete keeps the FK) — the
        # union math says yes, consumption says no, and the specific message
        # explains why.
        self.client.delete(f"/api/tournaments/{tid}/")
        r = self.client.post("/api/tournaments/", {"name": "Cup 3", "location": ""}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "quota_tournaments")
        self.assertIn("already attached", r.json()["detail"])

    def test_creator_plan_create_does_not_consume(self):
        self.buyer.plan = "creator"
        self.buyer.save(update_fields=["plan"])
        ent = self._pass(session="cs_pass2")
        r = self.client.post("/api/tournaments/", {"name": "Plain cup", "location": ""}, format="json")
        self.assertEqual(r.status_code, 201)
        ent.refresh_from_db()
        self.assertIsNone(ent.tournament_id)  # the pass keeps for later

    def test_game_limit_and_round_cap_at_attach(self):
        ent = self._pass(session="cs_pass3")
        r = self.client.post("/api/tournaments/", {"name": "Capped", "location": ""}, format="json")
        tid = r.json()["id"]
        body = {
            "mode": "points", "categories": [self.cat.id], "questions_per_category": 2,
            "tournament": tid, "round_number": 1,
        }
        self.assertEqual(self.client.post("/api/games/", body, format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/games/", body, format="json").status_code, 201)
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 403)
        payload = r.json()
        self.assertEqual(
            payload,
            {
                "detail": payload["detail"],
                "code": "quota_tournament_games",
                "used": 2,
                "limit": 2,
            },
        )
        # Round cap: a fresh pass-funded tournament rejects round 4.
        ent.game_limit = 6
        ent.save(update_fields=["game_limit"])
        body["round_number"] = 4
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("rounds 1–3", r.json()["round_number"][0].lower())

    def test_expired_pass_gates_attach_but_reads_stay(self):
        ent = self._pass(session="cs_pass4")
        r = self.client.post("/api/tournaments/", {"name": "Windowed", "location": ""}, format="json")
        tid = r.json()["id"]
        ent.refresh_from_db()
        ent.active_until = timezone.now() - timedelta(minutes=1)
        ent.save(update_fields=["active_until"])
        body = {
            "mode": "points", "categories": [self.cat.id], "questions_per_category": 2,
            "tournament": tid, "round_number": 1,
        }
        r = self.client.post("/api/games/", body, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "pack_inactive")
        # Detail stays readable forever.
        r = self.client.get(f"/api/tournaments/{tid}/")
        self.assertEqual(r.status_code, 200)


class BoardEmailTests(APITestCase):
    """§F1e (#18): the host's board-backup email — content, recipient, and
    the never-break-the-action contract."""

    def setUp(self):
        self.host = User.objects.create_user("mailhost@test.com", "sturdy-pass-123", plan="creator")
        self.cat = seed_category("Mailed", 3)
        self.client.force_authenticate(self.host)

    def _create(self):
        return self.client.post(
            "/api/games/",
            {"mode": "drinks", "categories": [self.cat.id], "questions_per_category": 3},
            format="json",
        )

    def test_email_carries_the_board_with_answers_to_the_host_only(self):
        from django.core import mail

        r = self._create()
        self.assertEqual(r.status_code, 201, r.content)
        code = r.json()["game"]["code"]
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["mailhost@test.com"])
        self.assertIn(code, message.subject)
        self.assertIn("Mailed", message.body)          # category name
        self.assertIn("SECRET-Mailed-0", message.body)  # ANSWERS ride along (host-only)
        self.assertIn("Mailed question 0?", message.body)
        self.assertIn("backup", message.body.lower())   # the v1 Game Backup footer

    def test_send_failure_never_breaks_the_create(self):
        from unittest import mock

        with mock.patch("games.emails.send_mail", side_effect=RuntimeError("smtp down")):
            r = self._create()
        self.assertEqual(r.status_code, 201, r.content)  # warn-and-continue
        self.assertEqual(Game.objects.count(), 1)
