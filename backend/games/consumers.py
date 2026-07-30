"""Realtime game channel.

Connect: ws://host/ws/game/<CODE>/?token=<participant_token>

Client -> server actions (JSON {"action": ..., ...}):
  player:
    buzz                       — register a buzz for the open question
    give_drink {target_participant_id}
                               — §G (Handoff #8): the winning team assigns
                                 the drink from their phone; any seat (host
                                 included, themselves included) is a target
  host only:
    start_game                 — lobby -> active
    open_cell {cell_id}        — reveal a question; buzzer stays LOCKED
    open_buzzer                — host finished reading; players may buzz
    lock_buzzer                — stop accepting buzzes
    reset_buzzer               — clear buzzes for current cell and re-lock
    reveal_answer              — send answer text to host + board screens
    judge {participant_id, correct}
                               — mark the first (or chosen) buzzer right/wrong;
                                 §F: judging CORRECT also performs the reveal
    assign_drinks {to_participant_id}
                               — host fallback for give_drink (same
                                 once-per-cell marker; first assignment wins)
    remove_player {participant_id}
                               — §F (Handoff #11): soft-remove a player seat
                                 (services.remove_player). Works in lobby and
                                 active games; the host seat is unremovable
    close_cell                 — return to board (marks cell answered)
    finish_game

Server -> clients events:
  state          — full snapshot (sent on connect and after every mutation)
  buzz           — incremental: someone buzzed {participant_id, name, order}
  answer_reveal  — {answer} (host action)
  error          — {detail} (+ {code} for documented structured errors, e.g.
                   §G's {"detail", "code": "drinks_already_assigned"} and
                   §F(#11)'s {"detail", "code": "player_removed"})

App close codes:
  4001 — unknown/expired participant token at connect (accept-then-close;
         Handoff #3 §E1). A REMOVED participant's token also lands here on
         any reconnect attempt (_get_participant filters removed seats).
  4003 — §F (Handoff #11): the sender's seat was removed mid-connection. An
         already-connected socket can't be closed by a connect-time filter,
         so every receive_json re-checks the seat is still active; a removed
         sender gets the `player_removed` structured error, then this close.

Design note: every mutation persists to the DB first, then a fresh snapshot is
broadcast. Clients are dumb renderers of state, so reloads/reconnects are
automatically consistent (requirement: "if a user reloads we don't want all
data lost"). Handoff #8 moved the last inline mutation bodies (buzz, judge,
buzzer open/lock/reset, drinks) into games/services.py so the suite exercises
exactly what the socket runs.
"""
import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .models import Game, GameStatus, Participant, ParticipantRole
from .serializers import GameStateSerializer
from .services import (
    ActionError,
    assign_drink,
    close_cell,
    finalize_game,
    judge_buzz,
    open_cell,
    register_buzz,
    remove_player,
    reset_buzzer,
    reveal_answer,
    set_buzzer,
)


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
        # §F (#11): a kicked player's live socket is already connected, so
        # the connect-time filter can't help — re-check the seat on every
        # message (one indexed query). Removed sender → structured error,
        # then the documented 4003 close (see module docstring).
        if not await self._participant_active():
            await self.send_json(
                {"type": "error", "detail": "The host removed this buzzer.", "code": "player_removed"}
            )
            await self.close(code=4003)
            return

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
            elif action == "give_drink":
                # §G: player-initiated. assign_drink validates that the sender
                # is the cell's judged-correct winner (the host seat comes in
                # via assign_drinks below but would pass too) and that the cell
                # is current + correct + not yet assigned (rule 4: the server
                # is the gate; the phone's disabled states are cosmetic).
                await self._assign_drink(content.get("target_participant_id"))
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
                # §F: services.judge_buzz — correct also reveals; both set the
                # snapshot's last_judgment marker.
                await self._host_judge(content.get("participant_id"), bool(content.get("correct")))
            elif action == "assign_drinks":
                # §G: the host fallback — same mutation, same once-per-cell
                # marker as the player path above.
                await self._assign_drink(content.get("to_participant_id"))
            elif action == "remove_player":
                # §F (#11): body in services.remove_player — soft flag, buzz/
                # cell cleanup for the open round, per the house rule.
                await self._host_remove_player(content.get("participant_id"))
            elif action == "close_cell":
                await self._host_close_cell()
            elif action == "finish_game":
                await self._host_finish_game()
            else:
                await self.send_json({"type": "error", "detail": f"Unknown action: {action}"})
                return
        except ActionError as exc:
            # Structured errors (§G's drinks_already_assigned) carry their
            # documented payload; plain ones stay {"detail"} exactly as before.
            payload = getattr(exc, "payload", None) or {"detail": str(exc)}
            await self.send_json({"type": "error", **payload})
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
        # §F (#11): a removed seat's token is dead — reconnects fall through
        # to the 4001 path exactly like an unknown token.
        return (
            Participant.objects.select_related("game")
            .filter(game__code=self.code, token=token, removed_at__isnull=True)
            .first()
        )

    @database_sync_to_async
    def _participant_active(self):
        return Participant.objects.filter(pk=self.participant.pk, removed_at__isnull=True).exists()

    @database_sync_to_async
    def _set_connected(self, value):
        Participant.objects.filter(pk=self.participant.pk).update(connected=value)

    @database_sync_to_async
    def _snapshot(self):
        game = (
            Game.objects.prefetch_related("columns__cells", "columns__category", "participants")
            # "host" feeds §H's brand — selected here so no snapshot pays an
            # extra query for it (the cells_remaining N+1 lesson).
            .select_related("current_cell__question", "judged_participant", "host")
            .get(code=self.code)
        )
        data = GameStateSerializer(game).data
        return json.loads(json.dumps(data, default=str))

    @database_sync_to_async
    def _handle_buzz(self):
        return register_buzz(code=self.code, participant=self.participant)

    @database_sync_to_async
    def _host_start_game(self):
        Game.objects.filter(code=self.code).update(status=GameStatus.ACTIVE, started_at=timezone.now())

    @database_sync_to_async
    def _host_open_cell(self, cell_id):
        # Body lives in services.open_cell (Handoff #6: shared with the test
        # suite; also clears answer_revealed for the fresh cell — rule 5 —
        # and, since #8, the §F judgment marker).
        open_cell(code=self.code, cell_id=cell_id)

    @database_sync_to_async
    def _host_set_buzzer(self, is_open):
        set_buzzer(code=self.code, is_open=is_open)

    @database_sync_to_async
    def _host_reset_buzzer(self):
        reset_buzzer(code=self.code)

    @database_sync_to_async
    def _host_reveal_answer(self):
        return reveal_answer(code=self.code)

    @database_sync_to_async
    def _host_judge(self, participant_id, correct):
        judge_buzz(code=self.code, participant_id=participant_id, correct=correct)

    @database_sync_to_async
    def _assign_drink(self, target_participant_id):
        # Shared by the player's give_drink and the host's assign_drinks —
        # services.assign_drink is the single gate (winner-only for players,
        # once per cell for everyone).
        assign_drink(code=self.code, actor=self.participant, target_participant_id=target_participant_id)

    @database_sync_to_async
    def _host_remove_player(self, participant_id):
        remove_player(code=self.code, participant_id=participant_id, actor=self.participant)

    @database_sync_to_async
    def _host_close_cell(self):
        close_cell(code=self.code)

    @database_sync_to_async
    def _host_finish_game(self):
        # Handoff #6 §G1: finishing now also computes + persists the outcome
        # (finished_at + winners, ties included) — see services.finalize_game.
        finalize_game(code=self.code)
