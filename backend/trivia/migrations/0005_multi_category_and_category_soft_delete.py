"""§F (Handoff #10) — HAND-WRITTEN migration. SHIPS IN THE ZIP (§D amendment).

The FK→M2M conversion needs a data migration sandwiched between schema steps,
which `makemigrations` cannot produce and which would lose every question's
category if skipped:

    1. AddField  Question.categories (M2M)          — schema
    2. RunPython copy each row's category_id into   — data
                 the M2M through-table
    3. RemoveField Question.category (the old FK)   — schema

Also in this set (§F5): Category.deleted_at, and the category-name unique
constraint becomes PARTIAL (active rows only) so a deleted name can be
reused. SQLite and Postgres both support partial unique constraints through
Django's condition=.

Forward path TESTED against a database seeded from the PRISTINE (pre-§F)
tree: seed_demo + a creator's own category/questions + a played game — after
migrating, every question keeps its category (now inside `categories`) and
the game's board columns/cells are untouched.

Rollback is OUT OF SCOPE: the reverse of the data copy raises on attempt
(documented in CHANGES.md). Restoring the FK from the M2M would have to
invent an answer for questions that by then live in several categories.
"""
from django.db import migrations, models


def copy_fk_into_m2m(apps, schema_editor):
    """Copy every Question.category_id into the new M2M through-table.

    Batched bulk_create on the through model — no per-row saves, no signals.
    At this point in the operation sequence the historical model still has
    BOTH fields (categories was just added; category is removed next).
    """
    Question = apps.get_model("trivia", "Question")
    Through = Question.categories.through
    Through.objects.bulk_create(
        (
            Through(question_id=question_id, category_id=category_id)
            for question_id, category_id in Question.objects.values_list("id", "category_id")
        ),
        batch_size=500,
    )


def refuse_reverse(apps, schema_editor):
    raise NotImplementedError(
        "The trivia FK→M2M category migration cannot be reversed: once a "
        "question lives in several categories there is no single category_id "
        "to restore. Restore from a database backup instead."
    )


class Migration(migrations.Migration):

    dependencies = [
        ("trivia", "0004_question_deleted_at_question_replaced_by"),
    ]

    operations = [
        # --- §F1: FK → M2M, three steps, data copy in the middle ----------
        migrations.AddField(
            model_name="question",
            name="categories",
            field=models.ManyToManyField(related_name="questions", to="trivia.category"),
        ),
        migrations.RunPython(copy_fk_into_m2m, refuse_reverse),
        migrations.RemoveField(
            model_name="question",
            name="category",
        ),
        # --- §F5: category soft delete + reusable names -------------------
        migrations.AddField(
            model_name="category",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="category",
            name="unique_category_name_per_owner",
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("owner", "name"),
                name="unique_category_name_per_owner",
            ),
        ),
    ]
