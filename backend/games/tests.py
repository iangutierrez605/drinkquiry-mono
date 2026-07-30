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

from rest_framework.test import APITestCase

from accounts.models import User
from trivia.models import Category, Question

from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import BoardCell, BoardColumn, Game, GameStatus, Participant, ParticipantRole
from .services import (
    ActionError,
    StructuredActionError,
    assign_drink,
    close_cell,
    create_game,
    finalize_game,
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
