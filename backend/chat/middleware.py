import jwt as PyJWT
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from django.conf import settings
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from urllib.parse import parse_qs
from rest_framework_simplejwt.tokens import UntypedToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
import logging

logger = logging.getLogger(__name__)

User = get_user_model()

@database_sync_to_async
def get_user_from_jwt(token_string):
    """Get user from JWT token for WebSocket authentication"""
    try:
        # First validate the token
        UntypedToken(token_string)
        
        # Decode the token to get user_id
        decoded_data = PyJWT.decode(token_string, settings.SECRET_KEY, algorithms=["HS256"])
        user_id = decoded_data.get("user_id")
        
        if user_id:
            user = User.objects.get(id=user_id)
            return user
        else:
            return AnonymousUser()
    except (InvalidToken, TokenError, PyJWT.DecodeError, User.DoesNotExist) as e:
        logger.error(f"JWT authentication failed: {str(e)}")
        return AnonymousUser()

class JWTAuthMiddleware(BaseMiddleware):
    """Custom middleware to handle JWT authentication for WebSocket connections"""
    
    async def __call__(self, scope, receive, send):
        # Parse query string to get token
        query_string = scope.get('query_string', b'').decode()
        query_params = parse_qs(query_string)
        token = query_params.get('token', [None])[0]
        
        # Authenticate user if token is provided
        if token:
            user = await get_user_from_jwt(token)
            scope['user'] = user
        else:
            scope['user'] = AnonymousUser()
        
        return await super().__call__(scope, receive, send)

def JWTAuthMiddlewareStack(inner):
    """Custom middleware stack that includes JWT authentication"""
    return JWTAuthMiddleware(inner)
