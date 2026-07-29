from rest_framework import serializers

from .models import BoardCell, BoardColumn, Buzz, Game, Participant


class ParticipantSerializer(serializers.ModelSerializer):
    # buzzer_sound (§I): 1-4, present in the snapshot and the join response so
    # clients read their sound from server state, never local guessing.
    class Meta:
        model = Participant
        fields = ("id", "name", "role", "score", "drinks_taken", "drinks_given", "connected", "buzzer_sound")


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
    """The currently open cell, with question content (never the answer)."""

    question_text = serializers.CharField(source="question.question_text", read_only=True)
    media_type = serializers.CharField(source="question.media_type", read_only=True)
    image = serializers.FileField(source="question.image", read_only=True)
    audio = serializers.FileField(source="question.audio", read_only=True)
    video = serializers.FileField(source="question.video", read_only=True)
    buzzes = BuzzSerializer(many=True, read_only=True)

    class Meta:
        model = BoardCell
        fields = ("id", "row", "value", "state", "question_text", "media_type", "image", "audio", "video", "buzzes")


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
    participants = ParticipantSerializer(many=True, read_only=True)
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

    class Meta:
        model = Game
        fields = (
            "code", "mode", "status", "questions_per_category", "max_players",
            "buzzer_open", "current_cell", "revealed_answer", "columns", "participants", "created_at",
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
            count = game.participants.filter(role="player").count()
        return count


class ReportQuestionSerializer(serializers.ModelSerializer):
    """A played (or unplayed) board cell in the post-game report — WITH the
    answer. Only ever serialized for finished games (the report view 409s
    otherwise), so rule 5 holds: no unrevealed answer can leak mid-game."""

    question_text = serializers.CharField(source="question.question_text", read_only=True)
    answer = serializers.CharField(source="question.answer", read_only=True)
    answered_by_name = serializers.CharField(source="answered_by.name", read_only=True, default=None)

    class Meta:
        model = BoardCell
        fields = ("id", "row", "value", "state", "question_text", "answer", "answered_by_name", "answered_correctly")


class ReportColumnSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    questions = ReportQuestionSerializer(source="cells", many=True, read_only=True)

    class Meta:
        model = BoardColumn
        fields = ("id", "position", "category_name", "questions")


class GameReportSerializer(serializers.ModelSerializer):
    """GET /api/games/{code}/report/ — full post-game detail, host-only."""

    participants = ParticipantSerializer(many=True, read_only=True)
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
