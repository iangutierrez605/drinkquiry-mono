from rest_framework import serializers

from .models import Category, MediaType, ModerationStatus, Question, Visibility


class PublicCategorySerializer(serializers.ModelSerializer):
    """§G1 (Handoff #12): the anonymous shop window — purpose-built, NOT a
    reuse of CategorySerializer (which leaks owner identity and moderation
    internals). Exactly five keys, now public API (pinned by test):
    {id, name, description, photo, question_count}. `question_count` is an
    annotation supplied by the view (one query, no N+1); NO question text,
    answers or ids ride this payload — counts only (rule 5 stays airtight).
    Photos are fine anonymously: official/approved content is moderated,
    and S3 mode signs URLs server-side regardless.
    """

    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        fields = ("id", "name", "description", "photo", "question_count")
        read_only_fields = fields


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
    # §F2 (Handoff #10): a question lives in one or more categories. Reads
    # and writes both use `categories` (list of ids). Deleted categories are
    # unassignable (§F5) — the queryset filter is the gate.
    # §K1 (Handoff #11): the deprecated single-`category` write alias (#10's
    # one-session back-compat) is GONE — a `category` key is now an unknown
    # field, and a body with no `categories` gets the 400 naming it (pinned).
    categories = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Category.objects.filter(deleted_at__isnull=True)
    )

    class Meta:
        model = Question
        fields = (
            "id", "categories", "question_text", "answer", "difficulty",
            "media_type", "image", "audio", "video",
            "owner", "visibility", "moderation_status", "moderation_note", "created_at",
        )
        read_only_fields = ("moderation_status", "moderation_note")

    def get_fields(self):
        # `categories` is required on create but optional on PATCH (an update
        # that doesn't mention categories keeps the existing set).
        fields = super().get_fields()
        if self.instance is not None:
            fields["categories"].required = False
        return fields

    def validate(self, attrs):
        media_type = attrs.get("media_type", getattr(self.instance, "media_type", MediaType.NONE))
        field_for_type = {MediaType.IMAGE: "image", MediaType.AUDIO: "audio", MediaType.VIDEO: "video"}
        expected = field_for_type.get(media_type)
        if expected and not (attrs.get(expected) or (self.instance and getattr(self.instance, expected))):
            raise serializers.ValidationError({expected: f"A {media_type} file is required for this media type."})
        categories = attrs.get("categories")
        if categories is not None:
            # De-dup while preserving order (a repeated id is harmless input).
            seen, unique = set(), []
            for cat in categories:
                if cat.pk not in seen:
                    seen.add(cat.pk)
                    unique.append(cat)
            attrs["categories"] = unique
            categories = unique
        elif self.instance is not None:
            categories = list(self.instance.categories.all())
        if not categories:
            raise serializers.ValidationError({"categories": "Pick at least one category."})
        # The permission rule is unchanged (official | own | publicly
        # visible) — now applied PER category: ONE ineligible category fails
        # the whole create/update.
        user = self.context["request"].user
        for category in categories:
            if not category.accepts_questions_from(user):
                raise serializers.ValidationError(
                    {"categories": f'You cannot add questions to "{category.name}".'}
                )
        return attrs

    def create(self, validated_data):
        categories = validated_data.pop("categories")
        wants_public = validated_data.get("visibility") == Visibility.PUBLIC
        question = Question(owner=self.context["request"].user, **validated_data)
        if wants_public:
            question.moderation_status = ModerationStatus.PENDING
        question.save()
        question.categories.set(categories)
        return question

    def update(self, instance, validated_data):
        categories = validated_data.pop("categories", None)
        wants_public = validated_data.get("visibility", instance.visibility) == Visibility.PUBLIC
        for key, value in validated_data.items():
            setattr(instance, key, value)
        # Any edit to public content re-enters the review queue.
        instance.moderation_status = ModerationStatus.PENDING if wants_public else ModerationStatus.NOT_SUBMITTED
        instance.save()
        if categories is not None:
            instance.categories.set(categories)
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
    # §F4 (Handoff #10): `category_name` became `category_names` (sorted list)
    # with the M2M — our serializer, our frontend, so REPLACED, not aliased.
    category_names = serializers.SerializerMethodField()
    # §I4: lifecycle context for the library — deleted_at (null = active) and
    # the id of the revision that superseded this row, if any. Read-only,
    # staff-only surface; the public QuestionSerializer never carries these.
    replaced_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta(QuestionSerializer.Meta):
        fields = QuestionSerializer.Meta.fields + (
            "owner_email", "owner_display_name", "category_names", "usage_count",
            "deleted_at", "replaced_by",
        )

    def get_category_names(self, obj):
        # Reads the prefetch (viewsets prefetch `categories`) — no N+1.
        return sorted(c.name for c in obj.categories.all())


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
