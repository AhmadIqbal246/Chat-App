import json
import base64
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from users.models import UserProfile
from .models import Conversation, Message
from .serializers import MessageSerializer, ConversationSerializer
from asgiref.sync import sync_to_async
from django.urls import reverse
from django.conf import settings
from django.utils import timezone
import random
from channels.db import database_sync_to_async
from django.shortcuts import get_object_or_404
from rest_framework_simplejwt.tokens import UntypedToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from urllib.parse import parse_qs
from .tasks import create_and_schedule_email_notification, cancel_pending_notifications_for_message

# JWT authentication is now handled by middleware

@database_sync_to_async
def generate_unique_phone_number():
    while True:
        number = f"03{random.randint(100000000, 999999999)}"
        if not UserProfile.objects.filter(phone_number=number).exists():
            return number

def schedule_email_notification_sync(message_id):
    """Schedule email notification task - synchronous version"""
    try:
        # Only try to schedule if Celery is working
        return create_and_schedule_email_notification.delay(message_id)
    except Exception as e:
        # Log the error but don't break the messaging functionality
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to schedule email notification for message {message_id}: {str(e)}")
        return None

def cancel_email_notifications_sync(message_id):
    """Cancel email notifications task - synchronous version"""
    try:
        # Only try to cancel if Celery is working
        return cancel_pending_notifications_for_message.delay(message_id)
    except Exception as e:
        # Log the error but don't break the messaging functionality
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to cancel email notifications for message {message_id}: {str(e)}")
        return None

@database_sync_to_async
def get_user_by_username(username):
    return User.objects.get(username=username)

@database_sync_to_async
def get_userprofile_by_user(user):
    return UserProfile.objects.get(user=user)

@database_sync_to_async
def get_conversation_by_id(conversation_id):
    return Conversation.objects.get(id=conversation_id)

@database_sync_to_async
def get_message_by_id(message_id):
    return get_object_or_404(Message, id=message_id)

@database_sync_to_async
def update_message_content(message, content):
    message.content = content.strip()
    message.save()

@database_sync_to_async
def delete_message(message):
    message.delete()

@database_sync_to_async
def get_messages_by_ids(message_ids, recipient_profile):
    return list(Message.objects.filter(
        id__in=message_ids,
        recipient=recipient_profile,
        is_read=False
    ))

@database_sync_to_async
def mark_messages_as_read(message_ids):
    return Message.objects.filter(id__in=message_ids).update(is_read=True)

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Get authenticated user from middleware
        self.user = self.scope.get('user')
        
        if self.user and not self.user.is_anonymous:
            self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
            self.room_group_name = f'chat_{self.conversation_id}'
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            
            # Only mark user as online when connecting if they have valid authentication
            # This prevents users from appearing online after they've logged out
            try:
                user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                # Only set online if user has a valid session/token - 
                # the middleware already validates the JWT token, so if we reach here, user is authenticated
                user_profile.is_online = True
                user_profile.last_seen = timezone.now()
                await database_sync_to_async(user_profile.save)(update_fields=['is_online', 'last_seen'])
            except UserProfile.DoesNotExist:
                pass  # User profile doesn't exist yet
            
            await self.accept()
        else:
            await self.close(code=4001)  # Unauthorized

    async def disconnect(self, close_code):
        # Mark user as offline when disconnecting
        if hasattr(self, 'user') and self.user and not self.user.is_anonymous:
            try:
                user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                user_profile.is_online = False
                user_profile.last_seen = timezone.now()
                await database_sync_to_async(user_profile.save)(update_fields=['is_online', 'last_seen'])
            except UserProfile.DoesNotExist:
                pass  # User profile doesn't exist yet
        
        # Only try to leave group if connection was established
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            action_type = data.get('action_type', 'send')
            content = data.get('content')
            sender_username = data.get('sender_username')
            message_type = data.get('message_type', 'text')
            audio_data_base64 = data.get('audio_data_base64')
            message_id = data.get('message_id')
            
            # Handle different action types
            if action_type == 'edit':
                await self.edit_message(data)
                return
            elif action_type == 'delete':
                await self.delete_message(data)
                return
            elif action_type == 'typing':
                await self.handle_typing(data)
                return
            elif action_type == 'stop_typing':
                await self.handle_stop_typing(data)
                return
            elif action_type == 'mark_read':
                await self.handle_mark_read(data)
                return
            
            # Validate required fields for message creation
            if not sender_username:
                await self.send(text_data=json.dumps({'error': 'sender_username is required'}))
                return
                
            if not content and message_type == 'text':
                await self.send(text_data=json.dumps({'error': 'content is required for text messages'}))
                return
                
            if message_type == 'audio' and not audio_data_base64:
                await self.send(text_data=json.dumps({'error': 'audio_data_base64 is required for audio messages'}))
                return

            # Find sender User
            sender_user = await database_sync_to_async(User.objects.get)(username=sender_username)
            
            # Get or create UserProfile for sender
            try:
                sender_profile = await database_sync_to_async(UserProfile.objects.get)(user=sender_user)
            except UserProfile.DoesNotExist:
                # Create profile if it doesn't exist (for Google users)
                phone = await generate_unique_phone_number()
                sender_profile = await database_sync_to_async(UserProfile.objects.create)(
                    user=sender_user,
                    phone_number=phone
                )

            # Fetch conversation
            conversation = await database_sync_to_async(Conversation.objects.get)(id=self.conversation_id)

            # Find recipient: all participants except the sender
            participants = await database_sync_to_async(lambda: list(conversation.participants.all()))()
            recipient_profiles = [p for p in participants if p.id != sender_profile.id]
            
            if not recipient_profiles:
                raise ValueError("No recipient found in conversation")
                
            recipient_profile = recipient_profiles[0]

            # Process audio data if present
            audio_data = None
            if message_type == 'audio' and audio_data_base64:
                import base64
                audio_data = base64.b64decode(audio_data_base64)

            # Create the message
            message = await database_sync_to_async(Message.objects.create)(
                conversation=conversation,
                sender=sender_user,
                recipient=recipient_profile,
                content=content,
                message_type=message_type,
                audio_data=audio_data
            )

            # Auto-restore conversation for participants who deleted it
            for participant in await database_sync_to_async(lambda: list(conversation.participants.all()))():
                if await database_sync_to_async(lambda: participant in conversation.deleted_by.all())():
                    await database_sync_to_async(conversation.deleted_by.remove)(participant)
                    # DO NOT clear deletion timestamp - keep it so user only sees messages after deletion
                    # The deletion timestamp should persist even after restoration

            # Build absolute URL for profile pictures
            base_url = f"{settings.BASE_API_URL}"
            sender_picture_url = None
            recipient_picture_url = None

            if sender_profile.profile_picture:
                sender_picture_url = f"{base_url}{sender_profile.profile_picture.url}"
            if recipient_profile and recipient_profile.profile_picture:
                recipient_picture_url = f"{base_url}{recipient_profile.profile_picture.url}"

            # Prepare response data
            response_data = {
                "id": message.id,
                "content": message.content,
                "sender_username": sender_username,
                "timestamp": message.timestamp.isoformat(),
                "is_delivered": message.is_delivered,
                "is_read": message.is_read,
                "sender_profile_picture": sender_picture_url,
                "recipient_profile_picture": recipient_picture_url,
                "message_type": message.message_type
            }
            
            # Add audio data if present
            if message.message_type == 'audio' and message.audio_data:
                import base64
                response_data["audio_data_base64"] = base64.b64encode(message.audio_data).decode('utf-8')

            # Email notifications are now handled automatically by Django signals

            # Broadcast to the group
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "message": response_data,
                }
            )
        except User.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'User not found'}))
        except Conversation.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'Conversation not found'}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event["message"]))
        
    async def edit_message(self, data):
        try:
            message_id = data.get('message_id')
            content = data.get('content')
            sender_username = data.get('sender_username')
            
            if not message_id or not content or not sender_username:
                await self.send(text_data=json.dumps({
                    'error': 'Missing required fields for editing message'
                }))
                return
                
            # Get the sender user
            sender_user = await get_user_by_username(sender_username)
            
            # Get the message and verify ownership
            message = await get_message_by_id(message_id)
            
            if message.sender.id != sender_user.id:
                await self.send(text_data=json.dumps({
                    'error': 'You do not have permission to edit this message'
                }))
                return
                
            # Verify message type is text
            if message.message_type != 'text':
                await self.send(text_data=json.dumps({
                    'error': 'Only text messages can be edited'
                }))
                return
                
            # Update the message
            await update_message_content(message, content)
            
            # Prepare response data
            response_data = {
                'action_type': 'edit',
                'id': message_id,
                'content': content.strip(),
                'sender_username': sender_username,
                'timestamp': message.timestamp.isoformat(),
                'message_type': 'text'
            }
            
            # Broadcast to the group
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': response_data,
                }
            )
            
        except User.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'User not found'}))
        except Message.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'Message not found'}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))
    
    async def delete_message(self, data):
        try:
            message_id = data.get('message_id')
            sender_username = data.get('sender_username')
            
            if not message_id or not sender_username:
                await self.send(text_data=json.dumps({
                    'error': 'Missing required fields for deleting message'
                }))
                return
                
            # Get the sender user
            sender_user = await get_user_by_username(sender_username)
            
            # Get the message and verify ownership
            message = await get_message_by_id(message_id)
            
            if message.sender.id != sender_user.id:
                await self.send(text_data=json.dumps({
                    'error': 'You do not have permission to delete this message'
                }))
                return
                
            # Delete the message
            await delete_message(message)
            
            # Prepare response data
            response_data = {
                'action_type': 'delete',
                'id': message_id,
                'sender_username': sender_username
            }
            
            # Broadcast to the group
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': response_data,
                }
            )
            
        except User.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'User not found'}))
        except Message.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'Message not found'}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))
    
    async def handle_typing(self, data):
        """Handle typing indicator"""
        try:
            sender_username = data.get('sender_username')
            if not sender_username:
                return
            
            # Broadcast typing indicator to the group
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_indicator',
                    'username': sender_username,
                    'is_typing': True
                }
            )
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))
    
    async def handle_stop_typing(self, data):
        """Handle stop typing indicator"""
        try:
            sender_username = data.get('sender_username')
            if not sender_username:
                return
            
            # Broadcast stop typing indicator to the group
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_indicator',
                    'username': sender_username,
                    'is_typing': False
                }
            )
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))
    
    async def handle_mark_read(self, data):
        """Handle marking messages as read"""
        try:
            reader_username = data.get('reader_username')
            message_ids = data.get('message_ids', [])
            
            if not reader_username or not message_ids:
                return
            
            # Get the reader user
            reader_user = await get_user_by_username(reader_username)
            reader_profile = await get_userprofile_by_user(reader_user)
            
            # Mark messages as read
            messages = await get_messages_by_ids(message_ids, reader_profile)
            
            if messages:
                # Update messages to read
                message_ids_to_update = [msg.id for msg in messages]
                await mark_messages_as_read(message_ids_to_update)
                
                # Cancel pending email notifications for read messages
                for message in messages:
                    await sync_to_async(cancel_email_notifications_sync)(message.id)
                for message in messages:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            'type': 'read_receipt',
                            'message_id': message.id,
                            'reader_username': reader_username
                        }
                    )
                    
        except User.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'User not found'}))
        except UserProfile.DoesNotExist:
            await self.send(text_data=json.dumps({'error': 'User profile not found'}))
        except Exception as e:
            await self.send(text_data=json.dumps({'error': str(e)}))
    
    async def typing_indicator(self, event):
        """Send typing indicator to WebSocket"""
        await self.send(text_data=json.dumps({
            'action_type': 'typing_indicator',
            'username': event['username'],
            'is_typing': event['is_typing']
        }))
    
    async def read_receipt(self, event):
        """Send read receipt to WebSocket"""
        await self.send(text_data=json.dumps({
            'action_type': 'read_receipt',
            'message_id': event['message_id'],
            'reader_username': event['reader_username']
        }))


class ConversationListConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Get authenticated user from middleware
        self.user = self.scope.get('user')
        
        if self.user and not self.user.is_anonymous:
            # Create a unique group for this user's conversation updates
            self.user_group_name = f'user_conversations_{self.user.id}'
            
            # Join user-specific conversation group
            await self.channel_layer.group_add(
                self.user_group_name,
                self.channel_name
            )
            
            # Mark user as online when connecting to conversation list (main presence indicator)
            try:
                user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                user_profile.is_online = True
                user_profile.last_seen = timezone.now()
                await database_sync_to_async(user_profile.save)(update_fields=['is_online', 'last_seen'])
            except UserProfile.DoesNotExist:
                pass  # User profile doesn't exist yet
            
            await self.accept()
        else:
            await self.close(code=4001)  # Unauthorized
    
    async def disconnect(self, close_code):
        # Mark user as offline when disconnecting from conversation list
        if hasattr(self, 'user') and self.user and not self.user.is_anonymous:
            try:
                user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                user_profile.is_online = False
                user_profile.last_seen = timezone.now()
                await database_sync_to_async(user_profile.save)(update_fields=['is_online', 'last_seen'])
            except UserProfile.DoesNotExist:
                pass  # User profile doesn't exist yet
        
        # Leave user-specific conversation group
        if hasattr(self, 'user_group_name'):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name
            )
    
    async def receive(self, text_data):
        # Handle ping/pong and heartbeat messages
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'ping':
                # Update last_seen time on heartbeat
                if hasattr(self, 'user') and self.user and not self.user.is_anonymous:
                    try:
                        user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                        user_profile.last_seen = timezone.now()
                        await database_sync_to_async(user_profile.save)(update_fields=['last_seen'])
                    except UserProfile.DoesNotExist:
                        pass
                
                await self.send(text_data=json.dumps({'type': 'pong'}))
            elif message_type == 'heartbeat':
                # Update user activity timestamp
                if hasattr(self, 'user') and self.user and not self.user.is_anonymous:
                    try:
                        user_profile = await database_sync_to_async(UserProfile.objects.get)(user=self.user)
                        user_profile.last_seen = timezone.now()
                        await database_sync_to_async(user_profile.save)(update_fields=['last_seen'])
                    except UserProfile.DoesNotExist:
                        pass
        except json.JSONDecodeError:
            pass
    
    # Handle conversation updates
    async def conversation_update(self, event):
        """Send conversation update to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'conversation_update',
            'conversation': event['conversation'],
            'is_new': event.get('is_new', False)
        }))
    
    # Handle conversation deletion
    async def conversation_delete(self, event):
        """Send conversation deletion to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'conversation_delete',
            'conversation_id': event['conversation_id']
        }))
