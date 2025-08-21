import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from urllib.parse import urlencode
from typing import Dict, Any
from django.shortcuts import redirect
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken
from .models import UserProfile



GOOGLE_ACCESS_TOKEN_OBTAIN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USER_INFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
LOGIN_URL = f'{settings.BASE_APP_URL}/internal/login'  # or home page

# 1. Get access token using code
def google_get_access_token(code: str, redirect_uri: str) -> str:
    data = {
        'code': code,
        'client_id': settings.GOOGLE_OAUTH2_CLIENT_ID,
        'client_secret': settings.GOOGLE_OAUTH2_CLIENT_SECRET,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code'
    }
    response = requests.post(GOOGLE_ACCESS_TOKEN_OBTAIN_URL, data=data)
    if not response.ok:
        raise ValidationError('Could not get access token from Google.')
    
    return response.json()['access_token']

# 2. Get user data from Google using token
def google_get_user_info(access_token: str) -> Dict[str, Any]:
    response = requests.get(GOOGLE_USER_INFO_URL, params={'access_token': access_token})
    if not response.ok:
        raise ValidationError('Could not get user info from Google.')
    
    return response.json()

# 3. Main function to call from the view
def get_user_data(validated_data):
    domain = settings.BASE_API_URL
    redirect_uri = f'{domain}/auth/api/login/google/'

    code = validated_data.get('code')
    error = validated_data.get('error')

    if error or not code:
        params = urlencode({'error': error})
        return redirect(f'{LOGIN_URL}?{params}')
    
    access_token = google_get_access_token(code=code, redirect_uri=redirect_uri)
    user_data = google_get_user_info(access_token=access_token)

    # Handle potential duplicate users for Google login
    email = user_data['email']
    
    # Try to get existing users with this email
    existing_users = User.objects.filter(email=email)
    
    if existing_users.exists():
        # If multiple users exist, use the most recent one
        user = existing_users.order_by('-date_joined').first()
        created = False
        
        # Update user info from Google if needed
        if user_data.get('given_name') and not user.first_name:
            user.first_name = user_data.get('given_name')
        if user_data.get('family_name') and not user.last_name:
            user.last_name = user_data.get('family_name')
        user.save()
    else:
        # Create new user
        user = User.objects.create(
            email=email,
            username=email,  # Use email as username for Google users
            first_name=user_data.get('given_name'),
            last_name=user_data.get('family_name')
        )
        created = True

    # Ensure profile exists even if user already existed
    profile, profile_created = UserProfile.objects.get_or_create(user=user)
    
    # Mark user as online when logging in via Google
    try:
        profile.set_online()
    except:
        pass  # Handle any potential errors

    # Generate JWT tokens instead of session
    refresh = RefreshToken.for_user(user)
    tokens = {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }

    return {
        'user': user,
        'tokens': tokens,
        'profile_data': {
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
        }
    }
