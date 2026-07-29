"""Realtime game channel.

Connect: ws://host/ws/game/<CODE>/?token=<participant_token>

Client -> server actions (JSON {"action": ..., ...}):
  player:
    buzz                       — register a buzz for the open question
  host only:
    start_game                 — lobby -> active
    open_cell {cell_id}        — reveal a question; buzzer stays LOCKED
    open_buzzer                — host finished reading; players may buzz
    lock_buzzer                — stop accepting buzzes
    reset_buzzer               — clear buzzes for current cell and re-lock
    reveal_answer              — send answer text to host + board screens
    judge {participant_id, correct}
                               — mark the first (or chosen) buzzer right/wrong
    assign_drinks {to_participant_id}
                               — winner makes another team drink cell.value
    close_cell                 — return to board (marks cell answered)
    finish_game

Server -> clients events:
  state          — full snapshot (sent on connect and after every mutation)
  buzz           — incremental: someone buzzed {participant_id, name, order}
  answer_reveal  — {answer} (host action)
  error          — {detail}

Design note: every mutation persists to the DB first, then a fresh snapshot is
broadcast. Clients are dumb renderers of state, so reloads/reconnects are
automatically consistent (requirement: "if a user reloads we don't want all
data lost").
"""
import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Buzz,
    DrinkAssignment,
    Game,
    GameMode,
    GameStatus,
    Participant,
    ParticipantRole,
)
from .serializers import GameStateSerializer
from .services import ActionError, close_cell, finalize_game, open_cell, reveal_answer


class GameConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.code = self.scope["url_route"]["kwargs"]["code"].upper()
        self.group = f"game_{self.code}"
        token = self._query_param("token")

        self.participant = await self._get_participant(token)
        if self.participant is None:
            # Handoff #3 §E1: accept the handshake, then close with the
            # documented app code. Before this, close() pre-accept surfaced as
            # an HTTP 403 rejection, which browsers report as an opaque 1006 —
            # the frontend hook handles both (its REST-probe fallback stays for
            # older deployments), but 4001 is the original contract.
            await self.accept()
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self._set_connected(True)
        await self._broadcast_state()

    async def disconnect(self, close_code):
        if getattr(self, "participant", None):
            await self._set_connected(False)
            await self.channel_layer.group_discard(self.group, self.channel_name)
            await self._broadcast_state()

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        is_host = self.participant.role == ParticipantRole.HOST

        try:
            if action == "buzz":
                order = await self._handle_buzz()
                # Handoff #3 §E2: the documented incremental event, sent just
                # before the snapshot broadcast below. The frontend prefers it
                # over synthesizing lastBuzz from snapshot diffs; the diff
                # fallback stays in the hook for older deployments.
                await self.channel_layer.group_send(
                    self.group,
                    {
                        "type": "game.event",
                        "payload": {
                            "type": "buzz",
                            "participant_id": self.participant.id,
                            "name": self.participant.name,
                            "order": order,
                        },
                    },
                )
            elif not is_host:
                await self.send_json({"type": "error", "detail": "Host-only action."})
                return
            elif action == "start_game":
                await self._host_start_game()
            elif action == "open_cell":
                await self._host_open_cell(content.get("cell_id"))
            elif action == "open_buzzer":
                await self._host_set_buzzer(True)
            elif action == "lock_buzzer":
                await self._host_set_buzzer(False)
            elif action == "reset_buzzer":
                await self._host_reset_buzzer()
            elif action == "reveal_answer":
                # Handoff #6 §F2: the reveal is now persisted (Game.answer_revealed)
                # so the public snapshot's `revealed_answer` field carries it to
                # REST-polling boards. The legacy `answer_reveal` event below is
                # unchanged (WS boards/hook keep using it — §B4-style dual path),
                # and we now ALSO fall through to the snapshot broadcast, per the
                # house pattern: persist first, then broadcast fresh state.
                answer = await self._host_reveal_answer()
                await self.channel_layer.group_send(
                    self.group, {"type": "game.event", "payload": {"type": "answer_reveal", "answer": answer}}
                )
            elif action == "judge":
                await self._host_judge(content.get("participant_id"), bool(content.get("correct")))
            elif action == "assign_drinks":
                await self._host_assign_drinks(content.get("to_participant_id"))
            elif action == "close_cell":
                await self._host_close_cell()
            elif action == "finish_game":
                await self._host_finish_game()
            else:
                await self.send_json({"type": "error", "detail": f"Unknown action: {action}"})
                return
        except ActionError as exc:
            await self.send_json({"type": "error", "detail": str(exc)})
            return

        await self._broadcast_state()

    # ---- group event handlers -------------------------------------------
    async def game_event(self, event):
        await self.send_json(event["payload"])

    # ---- helpers ---------------------------------------------------------
    def _query_param(self, key):
        query = self.scope.get("query_string", b"").decode()
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == key:
                    return v
        return None

    async def _broadcast_state(self):
        state = await self._snapshot()
        await self.channel_layer.group_send(
            self.group, {"type": "game.event", "payload": {"type": "state", "game": state}}
        )

    @database_sync_to_async
    def _get_participant(self, token):
        if not token:
            return None
        return Participant.objects.select_related("game").filter(game__code=self.code, token=token).first()

    @database_sync_to_async
    def _set_connected(self, value):
        Participant.objects.filter(pk=self.participant.pk).update(connected=value)

    @database_sync_to_async
    def _snapshot(self):
        game = (
            Game.objects.prefetch_related("columns__cells", "columns__category", "participants")
            .select_related("current_cell__question")
            .get(code=self.code)
        )
        data = GameStateSerializer(game).data
        return json.loads(json.dumps(data, default=str))

    @database_sync_to_async
    def _handle_buzz(self):
        with transaction.atomic():
            game = Game.objects.select_for_update().get(code=self.code)
            if not game.buzzer_open or game.current_cell_id is None:
                raise ActionError("Buzzer is locked.")
            if self.participant.role == ParticipantRole.HOST:
                raise ActionError("The host cannot buzz.")
            try:
                buzz = Buzz.objects.create(cell_id=game.current_cell_id, participant=self.participant)
            except IntegrityError:
                raise ActionError("You already buzzed.")
            order = Buzz.objects.filter(cell_id=game.current_cell_id, created_at__lte=buzz.created_at).count()
        return order

    @database_sync_to_async
    def _host_start_game(self):
        Game.objects.filter(code=self.code).update(status=GameStatus.ACTIVE, started_at=timezone.now())

    @database_sync_to_async
    def _host_open_cell(self, cell_id):
        # Body lives in services.open_cell (Handoff #6: shared with the test
        # suite; also clears answer_revealed for the fresh cell — rule 5).
        open_cell(code=self.code, cell_id=cell_id)

    @database_sync_to_async
    def _host_set_buzzer(self, is_open):
        with transaction.atomic():
            game = Game.objects.select_for_update().get(code=self.code)
            if game.current_cell_id is None:
                raise ActionError("Open a question first.")
            game.buzzer_open = is_open
            game.save(update_fields=["buzzer_open"])

    @database_sync_to_async
    def _host_reset_buzzer(self):
        with transaction.atomic():
            game = Game.objects.select_for_update().get(code=self.code)
            if game.current_cell_id is None:
                raise ActionError("No question is open.")
            Buzz.objects.filter(cell_id=game.current_cell_id).delete()
            game.buzzer_open = False
            game.save(update_fields=["buzzer_open"])

    @database_sync_to_async
    def _host_reveal_answer(self):
        return reveal_answer(code=self.code)

    @database_sync_to_async
    def _host_judge(self, participant_id, correct):
        with transaction.atomic():
            game = Game.objects.select_for_update().get(code=self.code)
            cell = game.current_cell
            if cell is None:
                raise ActionError("No question is open.")
            participant = game.participants.filter(pk=participant_id).first()
            if participant is None:
                raise ActionError("Unknown participant.")
            if correct:
                cell.answered_by = participant
                cell.answered_correctly = True
                cell.save(update_fields=["answered_by", "answered_correctly"])
                if game.mode == GameMode.POINTS:
                    participant.score += cell.value
                    participant.save(update_fields=["score"])
                game.buzzer_open = False
                game.save(update_fields=["buzzer_open"])
            else:
                if game.mode == GameMode.POINTS:
                    participant.score -= cell.value
                    participant.save(update_fields=["score"])
                # Wrong answer: that team is out of this round; reopen for others.
                game.buzzer_open = True
                game.save(update_fields=["buzzer_open"])

    @database_sync_to_async
    def _host_assign_drinks(self, to_participant_id):
        with transaction.atomic():
            game = Game.objects.select_for_update().get(code=self.code)
            cell = game.current_cell
            if cell is None or cell.answered_by_id is None or not cell.answered_correctly:
                raise ActionError("Drinks can only be assigned after a correct answer.")
            if game.mode != GameMode.DRINKS:
                raise ActionError("This game is in points mode.")
            target = game.participants.filter(pk=to_participant_id).exclude(pk=cell.answered_by_id).first()
            if target is None:
                raise ActionError("Pick another team to drink.")
            DrinkAssignment.objects.create(
                cell=cell, from_participant_id=cell.answered_by_id, to_participant=target, amount=cell.value
            )
            target.drinks_taken += cell.value
            target.save(update_fields=["drinks_taken"])
            winner = cell.answered_by
            winner.drinks_given += cell.value
            winner.score += cell.value  # drinks dealt double as the leaderboard
            winner.save(update_fields=["drinks_given", "score"])

    @database_sync_to_async
    def _host_close_cell(self):
        close_cell(code=self.code)

    @database_sync_to_async
    def _host_finish_game(self):
        # Handoff #6 §G1: finishing now also computes + persists the outcome
        # (finished_at + winners, ties included) — see services.finalize_game.
        finalize_game(code=self.code)
