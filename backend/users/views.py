from django.shortcuts import redirect
from django.conf import settings
from rest_framework.views import APIView
from .serializers import AuthSerializer
from .services import get_user_data
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.contrib.auth import logout, authenticate
from django.http import JsonResponse
from .serializers import PhoneNumberSerializer
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator
from .models import UserProfile
from .serializers import ManualSignupSerializer
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt,ensure_csrf_cookie, csrf_protect
from django.utils.decorators import method_decorator
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken


def create_jwt_response(user, message="Success"):
    """Create standardized JWT response with user data and tokens"""
    refresh = RefreshToken.for_user(user)
    
    # Get profile data
    try:
        profile = user.userprofile
        profile_data = {
            'phone_number': profile.phone_number,
            'profile_picture_url': profile.profile_picture_url,
            'bio': profile.bio,
            'date_of_birth': profile.date_of_birth,
            'gender': profile.gender,
            'gender_display': profile.get_gender_display() if profile.gender else "",
        }
    except:
        profile_data = {}
    
    return Response({
        'message': message,
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            **profile_data
        }
    })


class GoogleLoginApi(APIView):
    permission_classes = [AllowAny]
    def get(self, request, *args, **kwargs):
        try:
            auth_serializer = AuthSerializer(data=request.GET)
            auth_serializer.is_valid(raise_exception=True)

            validated_data = auth_serializer.validated_data
            data = get_user_data(validated_data)

            # Get JWT tokens instead of creating session
            user = data['user']
            tokens = data['tokens']
            access_token = tokens['access']
            refresh_token = tokens['refresh']
            
            # Redirect to frontend with JWT tokens as URL parameters
            redirect_url = f"{settings.BASE_APP_URL}/login?google_success=true&access={access_token}&refresh={refresh_token}"
            return redirect(redirect_url)
            
        except Exception as e:
            # Log the error and redirect to login with error message
            print(f"Google login error: {str(e)}")
            return redirect(f"{settings.BASE_APP_URL}/login?error=google_login_failed")



class CurrentUserApi(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        profile = user.userprofile  # Assuming you have a related UserProfile model
        return Response({
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone_number': profile.phone_number if profile else "",
            'profile_picture_url': profile.profile_picture_url if profile else "",
            'bio': profile.bio if profile else "",
            'date_of_birth': profile.date_of_birth if profile else None,
            'gender': profile.gender if profile else "",
            'gender_display': profile.get_gender_display() if profile and profile.gender else "",
        })




class LogoutApi(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, *args, **kwargs):
        try:
            # Mark user as offline when logging out
            try:
                user_profile = request.user.userprofile
                user_profile.set_offline()
            except:
                pass  # User profile doesn't exist or other error
            
            # Try to blacklist the refresh token if provided
            refresh_token = request.data.get("refresh")
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            # Also logout session (for backward compatibility)
            logout(request)
            return JsonResponse({'message': 'Logged out successfully'}, status=200)
        except (TokenError, InvalidToken):
            # Mark user as offline even if token blacklisting fails
            try:
                user_profile = request.user.userprofile
                user_profile.set_offline()
            except:
                pass  # User profile doesn't exist or other error
            
            # Even if token blacklisting fails, still logout successfully
            logout(request)
            return JsonResponse({'message': 'Logged out successfully'}, status=200)


class UpdateUserProfile(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        user = request.user
        profile = user.userprofile

        user.username = request.data.get("username", user.username)
        user.first_name = request.data.get("first_name", user.first_name)
        user.last_name = request.data.get("last_name", user.last_name)
        user.save()

        profile.phone_number = request.data.get("phone_number", profile.phone_number)
        profile.bio = request.data.get("bio", profile.bio)
        profile.gender = request.data.get("gender", profile.gender)
        
        # Handle date of birth
        date_of_birth = request.data.get("date_of_birth")
        if date_of_birth:
            profile.date_of_birth = date_of_birth

        # Handle profile picture upload
        if "profile_picture" in request.FILES:
            profile.profile_picture = request.FILES["profile_picture"]

        profile.save()

        # Return complete user data
        return Response({
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone_number': profile.phone_number,
            'profile_picture_url': profile.profile_picture_url,
            'bio': profile.bio,
            'date_of_birth': profile.date_of_birth,
            'gender': profile.gender,
            'gender_display': profile.get_gender_display() if profile.gender else "",
        })




@method_decorator(ensure_csrf_cookie, name='dispatch')
class GetCSRFToken(APIView):
    def get(self, request):
        return Response({'message': 'CSRF cookie set'})
    

class ManualSignupView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # Disable CSRF for JWT signup

    def post(self, request):
        serializer = ManualSignupSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            # Return JWT tokens instead of creating session
            return create_jwt_response(user, "Signup successful")
        return Response(serializer.errors, status=400)
    


class ManualLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # Disable CSRF for JWT login
    
    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({'detail': 'User with this email does not exist.'}, status=400)

        user = authenticate(request, username=user.username, password=password)

        if user is not None:
            # Mark user as online when logging in
            try:
                user_profile = user.userprofile
                user_profile.set_online()
            except:
                pass  # User profile doesn't exist or other error
            
            # Return JWT tokens instead of creating session
            return create_jwt_response(user, "Login successful")
        else:
            return Response({'detail': 'Invalid password'}, status=400)
