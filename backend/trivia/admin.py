from django.contrib import admin

from .models import Category, ModerationStatus, Question, Theme


@admin.action(description="Approve selected for public listing")
def approve(modeladmin, request, queryset):
    queryset.update(moderation_status=ModerationStatus.APPROVED)


@admin.action(description="Reject selected")
def reject(modeladmin, request, queryset):
    queryset.update(moderation_status=ModerationStatus.REJECTED)


class ModerationAdminMixin:
    actions = (approve, reject)
    list_filter = ("visibility", "moderation_status")


@admin.register(Category)
class CategoryAdmin(ModerationAdminMixin, admin.ModelAdmin):
    list_display = ("name", "owner", "visibility", "moderation_status", "created_at")
    search_fields = ("name",)


@admin.register(Question)
class QuestionAdmin(ModerationAdminMixin, admin.ModelAdmin):
    # §F (Handoff #10): `category` FK became the `categories` M2M — the list
    # shows a joined name column; the filter targets the M2M (admin renders
    # it as the same category dropdown).
    list_display = ("question_text", "category_names", "difficulty", "media_type", "owner", "visibility", "moderation_status")
    search_fields = ("question_text", "answer")
    list_filter = ModerationAdminMixin.list_filter + ("media_type", "categories")

    @admin.display(description="Categories")
    def category_names(self, obj):
        return ", ".join(sorted(c.name for c in obj.categories.all())) or "—"


@admin.register(Theme)
class ThemeAdmin(admin.ModelAdmin):
    # §G: staff-curated tag table; the real management UI is /moderate's
    # Themes tab — this is the escape hatch.
    list_display = ("name", "created_at", "deleted_at")
    search_fields = ("name",)
    filter_horizontal = ("categories",)
