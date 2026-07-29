from django.contrib import admin

from .models import BoardCell, BoardColumn, Buzz, DrinkAssignment, Game, Participant


class ColumnInline(admin.TabularInline):
    model = BoardColumn
    extra = 0


class ParticipantInline(admin.TabularInline):
    model = Participant
    extra = 0
    readonly_fields = ("token",)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ("code", "host", "mode", "status", "created_at")
    list_filter = ("mode", "status")
    inlines = (ColumnInline, ParticipantInline)


admin.site.register(BoardCell)
admin.site.register(Buzz)
admin.site.register(DrinkAssignment)
