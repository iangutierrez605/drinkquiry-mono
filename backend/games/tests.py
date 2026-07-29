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

from .models import Game, GameStatus, Participant, ParticipantRole
from .services import ActionError, close_cell, create_game, finalize_game, open_cell, reveal_answer


def seed_category(name: str, n_questions: int = 5) -> Category:
    """Official (owner-less) category with n usable questions; answers are
    distinctive strings we can grep entire payloads for."""
    cat = Category.objects.create(owner=None, name=name)
    for i in range(n_questions):
        Question.objects.create(
            category=cat,
            owner=None,
            question_text=f"{name} question {i}?",
            answer=f"SECRET-{name}-{i}",
            difficulty=min(i + 1, 5),
        )
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
        self.assertEqual(by_name["Team A"]["buzzer_sound"], 1)
        self.assertIn("buzzer_sound", by_name["Host"])  # host seat has one, never plays it
