"""
Auto-generates a fun, anonymous display name for new users — movie/show
themed so it fits the site, e.g. "MysticPopcorn42", "ShadowDirector7".
Nobody's real name/email is ever shown publicly; this is what appears
everywhere instead (Gist, reviews, profile pages).
"""
import random

ADJECTIVES = [
    'Mystic', 'Cosmic', 'Neon', 'Shadow', 'Golden', 'Electric', 'Crimson',
    'Frosty', 'Lunar', 'Wild', 'Silent', 'Rogue', 'Velvet', 'Turbo', 'Sneaky',
    'Cinematic', 'Dramatic', 'Epic', 'Vintage', 'Retro', 'Midnight', 'Rebel',
    'Phantom', 'Iron', 'Crimson', 'Sly', 'Bold', 'Chill', 'Sonic', 'Star',
]

NOUNS = [
    'Popcorn', 'Director', 'Critic', 'Cameo', 'Reel', 'Screen', 'Premiere',
    'Panda', 'Fox', 'Tiger', 'Falcon', 'Wolf', 'Raven', 'Otter', 'Phoenix',
    'Comet', 'Ninja', 'Wizard', 'Rider', 'Pirate', 'Astronaut', 'Villain',
    'Hero', 'Spotlight', 'Trailer', 'Marathon', 'Binger', 'Popcorn', 'Ticket',
]


def generate_nickname():
    """A unique, movie-themed display name. Falls back to a plain numbered
    handle in the astronomically unlikely case 20 random tries all collide."""
    from .models import Profile
    for _ in range(20):
        name = f"{random.choice(ADJECTIVES)}{random.choice(NOUNS)}{random.randint(1, 999)}"
        if not Profile.objects.filter(display_name=name).exists():
            return name
    return f"Watcher{random.randint(10000, 99999)}"
