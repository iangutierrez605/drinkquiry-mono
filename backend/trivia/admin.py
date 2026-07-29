from django.contrib import admin

from .models import Category, ModerationStatus, Question


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
    list_display = ("question_text", "category", "difficulty", "media_type", "owner", "visibility", "moderation_status")
    search_fields = ("question_text", "answer")
    list_filter = ModerationAdminMixin.list_filter + ("media_type", "category")
