from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from users.models import UserProfile
from users.signals import generate_unique_phone_number

class Command(BaseCommand):
    help = 'Creates UserProfile for users that do not have one'

    def handle(self, *args, **options):
        users_without_profile = User.objects.filter(userprofile__isnull=True)
        created_count = 0
        
        for user in users_without_profile:
            phone = generate_unique_phone_number()
            UserProfile.objects.create(user=user, phone_number=phone)
            created_count += 1
            
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully created {created_count} user profiles'
            )
        ) 