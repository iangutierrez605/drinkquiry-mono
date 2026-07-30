from rest_framework import serializers

from .models import Category, MediaType, ModerationStatus, Question, Visibility


class CategorySerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    usable_question_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = (
            "id", "name", "description", "photo", "owner",
            "visibility", "moderation_status", "moderation_note",
            "usable_question_count", "created_at",
        )
        read_only_fields = ("moderation_status", "moderation_note")

    def get_usable_question_count(self, obj):
        request = self.context.get("request")
        return obj.usable_question_count(request.user if request else None)

    def validate_visibility(self, value):
        return value

    def create(self, validated_data):
        return self._apply_visibility_rules(validated_data, instance=None)

    def update(self, instance, validated_data):
        return self._apply_visibility_rules(validated_data, instance=instance)

    def _apply_visibility_rules(self, validated_data, instance):
        wants_public = validated_data.get("visibility") == Visibility.PUBLIC
        if instance is None:
            obj = Category(owner=self.context["request"].user, **validated_data)
        else:
            for key, value in validated_data.items():
                setattr(instance, key, value)
            obj = instance
        if wants_public:
            # Public content must be vetted before it is actually public.
            obj.moderation_status = ModerationStatus.PENDING
        else:
            obj.visibility = Visibility.PRIVATE
            obj.moderation_status = ModerationStatus.NOT_SUBMITTED
        obj.save()
        return obj


class QuestionSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Question
        fields = (
            "id", "category", "question_text", "answer", "difficulty",
            "media_type", "image", "audio", "video",
            "owner", "visibility", "moderation_status", "moderation_note", "created_at",
        )
        read_only_fields = ("moderation_status", "moderation_note")

    def validate(self, attrs):
        media_type = attrs.get("media_type", getattr(self.instance, "media_type", MediaType.NONE))
        field_for_type = {MediaType.IMAGE: "image", MediaType.AUDIO: "audio", MediaType.VIDEO: "video"}
        expected = field_for_type.get(media_type)
        if expected and not (attrs.get(expected) or (self.instance and getattr(self.instance, expected))):
            raise serializers.ValidationError({expected: f"A {media_type} file is required for this media type."})
        category = attrs.get("category") or (self.instance and self.instance.category)
        user = self.context["request"].user
        if category and not category.accepts_questions_from(user):
            raise serializers.ValidationError({"category": "You cannot add questions to this category."})
        return attrs

    def create(self, validated_data):
        wants_public = validated_data.get("visibility") == Visibility.PUBLIC
        question = Question(owner=self.context["request"].user, **validated_data)
        if wants_public:
            question.moderation_status = ModerationStatus.PENDING
        question.save()
        return question

    def update(self, instance, validated_data):
        wants_public = validated_data.get("visibility", instance.visibility) == Visibility.PUBLIC
        for key, value in validated_data.items():
            setattr(instance, key, value)
        # Any edit to public content re-enters the review queue.
        instance.moderation_status = ModerationStatus.PENDING if wants_public else ModerationStatus.NOT_SUBMITTED
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# Moderation review serializers (staff-only endpoints, read + action responses)
# ---------------------------------------------------------------------------

class _OwnerContextMixin(serializers.Serializer):
    """Adds who-owns-this context for reviewers. Owner is null for official
    content (which never enters the queue, but history views may still list it)."""

    owner_email = serializers.SerializerMethodField()
    owner_display_name = serializers.SerializerMethodField()

    def get_owner_email(self, obj):
        return obj.owner.email if obj.owner_id else None

    def get_owner_display_name(self, obj):
        return obj.owner.display_name if obj.owner_id else None


class _UsageCountMixin(serializers.Serializer):
    """§J1 (Handoff #8): how often this item has appeared in games, derived
    from existing rows — cells referencing a question / board columns
    referencing a category (BoardColumn is unique per (game, category), so
    the column count IS the game count). The viewsets annotate `usage_count`
    to avoid an N+1; direct serializer use falls back to counting."""

    usage_count = serializers.SerializerMethodField()

    def get_usage_count(self, obj):
        count = getattr(obj, "usage_count", None)
        if count is None:
            relation = "boardcolumn_set" if hasattr(obj, "boardcolumn_set") else "boardcell_set"
            count = getattr(obj, relation).count()
        return count


class ModerationCategorySerializer(_UsageCountMixin, _OwnerContextMixin, CategorySerializer):
    class Meta(CategorySerializer.Meta):
        fields = CategorySerializer.Meta.fields + ("owner_email", "owner_display_name", "usage_count")


class ModerationQuestionSerializer(_UsageCountMixin, _OwnerContextMixin, QuestionSerializer):
    # `answer` is already in QuestionSerializer — required for review. It never
    # leaks into game snapshots (OpenCellSerializer excludes it).
    category_name = serializers.CharField(source="category.name", read_only=True)
    # §I4: lifecycle context for the library — deleted_at (null = active) and
    # the id of the revision that superseded this row, if any. Read-only,
    # staff-only surface; the public QuestionSerializer never carries these.
    replaced_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta(QuestionSerializer.Meta):
        fields = QuestionSerializer.Meta.fields + (
            "owner_email", "owner_display_name", "category_name", "usage_count",
            "deleted_at", "replaced_by",
        )


class FlaggedQuestionSerializer(ModerationQuestionSerializer):
    """§K2: one row in the /moderate "Flagged" tab — the question (with the
    reviewer context + §J1 usage count above) plus its OPEN reports. The
    viewset prefetches open reports as `open_reports`."""

    reports = serializers.SerializerMethodField()
    report_count = serializers.SerializerMethodField()

    class Meta(ModerationQuestionSerializer.Meta):
        fields = ModerationQuestionSerializer.Meta.fields + ("reports", "report_count")

    def _open_reports(self, obj):
        return getattr(obj, "open_reports", None) or [
            r for r in obj.reports.all() if r.status == "open"
        ]

    def get_report_count(self, obj):
        return len(self._open_reports(obj))

    def get_reports(self, obj):
        return [
            {
                "id": r.id,
                "reporter_email": r.reporter.email,
                "reporter_display_name": r.reporter.display_name,
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in self._open_reports(obj)
        ]
