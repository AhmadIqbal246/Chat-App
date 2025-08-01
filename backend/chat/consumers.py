import json
import base64
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from users.models import UserProfile
from .models import Conversation, Message
from .serializers import MessageSerializer
from asgiref.sync import sync_to_async
from django.urls import reverse
from django.conf import settings
import random
from channels.db import database_sync_to_async
from django.shortcuts import get_object_or_404

def generate_unique_phone_number():
    while True:
        number = f"03{random.randint(100000000, 999999999)}"
        if not UserProfile.objects.filter(phone_number=number).exists():
            return number

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.room_group_name = f'chat_{self.conversation_id}'
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
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

            # Find sender User
            sender_user = await sync_to_async(User.objects.get)(username=sender_username)
            
            # Get or create UserProfile for sender
            try:
                sender_profile = await sync_to_async(UserProfile.objects.get)(user=sender_user)
            except UserProfile.DoesNotExist:
                # Create profile if it doesn't exist (for Google users)
                phone = await sync_to_async(generate_unique_phone_number)()
                sender_profile = await sync_to_async(UserProfile.objects.create)(
                    user=sender_user,
                    phone_number=phone
                )

            # Fetch conversation
            conversation = await sync_to_async(Conversation.objects.get)(id=self.conversation_id)

            # Find recipient: all participants except the sender
            participants = await sync_to_async(lambda: list(conversation.participants.all()))()
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
            message = await sync_to_async(Message.objects.create)(
                conversation=conversation,
                sender=sender_user,
                recipient=recipient_profile,
                content=content,
                message_type=message_type,
                audio_data=audio_data
            )

            # Auto-restore conversation for participants who deleted it
            for participant in await sync_to_async(lambda: list(conversation.participants.all()))():
                if await sync_to_async(lambda: participant in conversation.deleted_by.all())():
                    await sync_to_async(conversation.deleted_by.remove)(participant)
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
            sender_user = await sync_to_async(User.objects.get)(username=sender_username)
            
            # Get the message and verify ownership
            message = await sync_to_async(lambda: get_object_or_404(Message, id=message_id))()
            message_sender = await sync_to_async(lambda: message.sender)()
            
            if message_sender.id != sender_user.id:
                await self.send(text_data=json.dumps({
                    'error': 'You do not have permission to edit this message'
                }))
                return
                
            # Verify message type is text
            message_type = await sync_to_async(lambda: message.message_type)()
            if message_type != 'text':
                await self.send(text_data=json.dumps({
                    'error': 'Only text messages can be edited'
                }))
                return
                
            # Update the message
            await sync_to_async(lambda: setattr(message, 'content', content.strip()))()
            await sync_to_async(message.save)()
            
            # Prepare response data
            response_data = {
                'action_type': 'edit',
                'id': message_id,
                'content': content.strip(),
                'sender_username': sender_username,
                'timestamp': await sync_to_async(lambda: message.timestamp.isoformat())(),
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
            sender_user = await sync_to_async(User.objects.get)(username=sender_username)
            
            # Get the message and verify ownership
            message = await sync_to_async(lambda: get_object_or_404(Message, id=message_id))()
            message_sender = await sync_to_async(lambda: message.sender)()
            
            if message_sender.id != sender_user.id:
                await self.send(text_data=json.dumps({
                    'error': 'You do not have permission to delete this message'
                }))
                return
                
            # Delete the message
            await sync_to_async(message.delete)()
            
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