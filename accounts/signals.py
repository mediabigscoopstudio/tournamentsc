from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PlayerProfile, User


@receiver(post_save, sender=User)
def ensure_player_profile(sender, instance, created, **kwargs):
    """Every account gets a PlayerProfile — being a player is the baseline
    capability. Organizer capability is added separately via approval."""
    if created and not instance.is_staff:
        PlayerProfile.objects.get_or_create(user=instance)
