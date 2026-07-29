"""Seed official demo categories/questions so a game can be created immediately."""
from django.core.management.base import BaseCommand

from trivia.models import Category, ModerationStatus, Question, Visibility

DEMO = {
    "Movies": [
        ("Which 1994 film features the line 'Life is like a box of chocolates'?", "Forrest Gump"),
        ("Who directed Jurassic Park?", "Steven Spielberg"),
        ("What is the highest-grossing film of all time (unadjusted)?", "Avatar"),
        ("Which movie won Best Picture in 2020?", "Parasite"),
        ("What planet is Luke Skywalker from?", "Tatooine"),
    ],
    "Science": [
        ("What gas do plants absorb from the atmosphere?", "Carbon dioxide"),
        ("What is the chemical symbol for gold?", "Au"),
        ("How many bones are in the adult human body?", "206"),
        ("What is the speed of light in km/s (approx)?", "300,000"),
        ("What particle carries a negative charge?", "Electron"),
    ],
    "Geography": [
        ("What is the capital of Australia?", "Canberra"),
        ("Which river is the longest in the world?", "The Nile"),
        ("How many continents are there?", "7"),
        ("Which country has the most time zones?", "France"),
        ("What is the smallest country in the world?", "Vatican City"),
    ],
    "Music": [
        ("Which band released 'Bohemian Rhapsody'?", "Queen"),
        ("What instrument has 88 keys?", "Piano"),
        ("Who is known as the King of Pop?", "Michael Jackson"),
        ("Which artist released the album '1989'?", "Taylor Swift"),
        ("What does DJ stand for?", "Disc jockey"),
    ],
    "Food & Drink": [
        ("What is the main ingredient in guacamole?", "Avocado"),
        ("Which country invented pizza?", "Italy"),
        ("What spirit is in a margarita?", "Tequila"),
        ("What is sushi traditionally wrapped in?", "Nori (seaweed)"),
        ("Which nut is used to make marzipan?", "Almond"),
    ],
}


class Command(BaseCommand):
    help = "Seed official demo categories and questions"

    def handle(self, *args, **options):
        for name, questions in DEMO.items():
            category, _ = Category.objects.get_or_create(
                owner=None,
                name=name,
                defaults={
                    "visibility": Visibility.PUBLIC,
                    "moderation_status": ModerationStatus.APPROVED,
                    "description": f"Official {name} pack",
                },
            )
            for difficulty, (text, answer) in enumerate(questions, start=1):
                Question.objects.get_or_create(
                    category=category,
                    question_text=text,
                    defaults={
                        "answer": answer,
                        "difficulty": difficulty,
                        "owner": None,
                        "visibility": Visibility.PUBLIC,
                        "moderation_status": ModerationStatus.APPROVED,
                    },
                )
        self.stdout.write(self.style.SUCCESS("Seeded demo categories and questions."))
