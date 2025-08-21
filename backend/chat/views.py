from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from users.models import UserProfile
from .models import Conversation, Message
from .serializers import MessageSerializer, ConversationSerializer
from django.shortcuts import get_object_or_404
from .utils import send_conversation_update, send_conversation_delete
from .tasks import create_and_schedule_email_notification
import json
from django.utils import timezone
class SendMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        sender = request.user
        recipient_phone = request.data.get('recipient_phone')
        content = request.data.get('content')
        message_type = request.data.get('message_type', 'text')
        audio_data_base64 = request.data.get('audio_data_base64')

        # For text messages, content is required. For audio messages, audio_data_base64 is required
        if message_type == 'text' and not content:
            return Response({"error": "Content is required for text messages."}, status=status.HTTP_400_BAD_REQUEST)
        elif message_type == 'audio' and not audio_data_base64:
            return Response({"error": "Audio data is required for audio messages."}, status=status.HTTP_400_BAD_REQUEST)
        
        if not recipient_phone:
            return Response({"error": "Recipient phone is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            recipient_profile = UserProfile.objects.get(phone_number=recipient_phone)
        except UserProfile.DoesNotExist:
            return Response({"error": "Recipient not found."}, status=status.HTTP_404_NOT_FOUND)

        # Get or create conversation between the two users
        conversation = (
            Conversation.objects
            .filter(participants=sender.userprofile)
            .filter(participants=recipient_profile)
            .first()
        )
        is_new_conversation = False
        if not conversation:
            conversation = Conversation.objects.create()
            conversation.participants.add(sender.userprofile, recipient_profile)
            conversation.save()
            is_new_conversation = True

        # Process audio data if present
        audio_data = None
        if message_type == 'audio' and audio_data_base64:
            import base64
            try:
                audio_data = base64.b64decode(audio_data_base64)
            except Exception as e:
                return Response({"error": f"Invalid audio data: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Create the message
        message = Message.objects.create(
            conversation=conversation,
            sender=sender,
            recipient=recipient_profile,
            content=content or "Audio message",
            message_type=message_type,
            audio_data=audio_data
        )

        # Auto-restore conversation for participants who deleted it
        for participant in conversation.participants.all():
            if participant in conversation.deleted_by.all():
                conversation.deleted_by.remove(participant)
                # DO NOT clear deletion timestamp - keep it so user only sees messages after deletion

        # Email notifications are now handled automatically by Django signals
        
        # Send real-time conversation update to all participants
        send_conversation_update(conversation, is_new=is_new_conversation, request=request)
        
        # Serialize response data
        message_serializer = MessageSerializer(message, context={'request': request})
        conversation_serializer = ConversationSerializer(conversation, context={'request': request})
        
        response_data = {
            'message': message_serializer.data,
            'conversation': conversation_serializer.data,
            'conversation_id': conversation.id,
            'is_new_conversation': is_new_conversation
        }
        
        return Response(response_data, status=status.HTTP_201_CREATED)

class UserConversationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_profile = request.user.userprofile
        
        # Get pagination parameters
        page = int(request.GET.get('page', 1))
        page_size = int(request.GET.get('page_size', 8))  # Default 8 conversations per page
        
        # Calculate offset
        offset = (page - 1) * page_size
        
        # Base query for conversations
        conversations_query = Conversation.objects.filter(
            participants=user_profile
        ).exclude(
            deleted_by=user_profile
        ).order_by('-updated_at')
        
        # Get total count for pagination info
        total_conversations = conversations_query.count()
        
        # Get paginated conversations
        conversations = conversations_query[offset:offset + page_size]
        
        serializer = ConversationSerializer(conversations, many=True, context={'request': request})
        
        # Calculate pagination metadata
        total_pages = (total_conversations + page_size - 1) // page_size
        has_next = page < total_pages
        has_previous = page > 1
        
        return Response({
            'conversations': serializer.data,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total_conversations': total_conversations,
                'total_pages': total_pages,
                'has_next': has_next,
                'has_previous': has_previous
            }
        })

class ConversationMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        try:
            conversation = Conversation.objects.get(id=conversation_id, participants=request.user.userprofile)
            
            # Get pagination parameters
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 50))  # Default 50 messages per page
            
            # Calculate offset
            offset = (page - 1) * page_size
            
            # Get messages based on user's deletion timestamp
            messages_query = Message.objects.filter(conversation=conversation)
            
            # Check if user has a deletion timestamp
            deletion_timestamps = conversation.deletion_timestamps or {}
            user_deletion_time = deletion_timestamps.get(str(request.user.userprofile.id))
            
            if user_deletion_time:
                # User deleted the conversation, only show messages after deletion
                from django.utils import timezone
                deletion_datetime = timezone.datetime.fromisoformat(user_deletion_time)
                messages_query = messages_query.filter(timestamp__gt=deletion_datetime)
            
            # Get total count for pagination info
            total_messages = messages_query.count()
            
            # Order by timestamp descending for pagination (newest first for loading)
            # Then reverse the results to show oldest first in UI
            messages = list(messages_query.order_by('-timestamp')[offset:offset + page_size])
            messages.reverse()  # Reverse to show chronological order
            
            # Mark messages as read (only for current page)
            message_ids = [msg.id for msg in messages]
            Message.objects.filter(
                id__in=message_ids,
                recipient=request.user.userprofile,
                is_read=False
            ).update(is_read=True)
            
            serializer = MessageSerializer(messages, many=True, context={'request': request})
            
            # Calculate pagination metadata
            total_pages = (total_messages + page_size - 1) // page_size
            has_next = page < total_pages
            has_previous = page > 1
            
            return Response({
                'messages': serializer.data,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total_messages': total_messages,
                    'total_pages': total_pages,
                    'has_next': has_next,
                    'has_previous': has_previous
                }
            })
        except Conversation.DoesNotExist:
            return Response({"error": "Conversation not found or access denied."}, status=404)

class SendMessageInConversationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        user = request.user
        user_profile = user.userprofile
        content = request.data.get("content", "")
        message_type = request.data.get("message_type", "text")
        audio_data_base64 = request.data.get("audio_data_base64")

        # For audio messages, content can be empty but audio_data_base64 is required
        if message_type == "text" and not content:
            return Response({"error": "Message content is required for text messages."}, status=status.HTTP_400_BAD_REQUEST)
        elif message_type == "audio" and not audio_data_base64:
            return Response({"error": "Audio data is required for audio messages."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            conversation = Conversation.objects.get(id=conversation_id, participants=user_profile)
        except Conversation.DoesNotExist:
            return Response({"error": "Conversation not found or access denied."}, status=status.HTTP_404_NOT_FOUND)

        # Find recipient: all participants except the sender
        recipients = conversation.participants.exclude(id=user_profile.id)
        if not recipients.exists():
            return Response({"error": "No recipient found."}, status=status.HTTP_400_BAD_REQUEST)
        recipient_profile = recipients.first()  # For 1-to-1 chats

        # Process audio data if present
        audio_data = None
        if message_type == 'audio' and audio_data_base64:
            import base64
            try:
                audio_data = base64.b64decode(audio_data_base64)
            except Exception as e:
                return Response({"error": f"Invalid audio data: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        message = Message.objects.create(
            conversation=conversation,
            sender=user,
            recipient=recipient_profile,
            content=content,
            message_type=message_type,
            audio_data=audio_data
        )

        # Auto-restore conversation for participants who deleted it
        for participant in conversation.participants.all():
            if participant in conversation.deleted_by.all():
                conversation.deleted_by.remove(participant)
                # DO NOT clear deletion timestamp - keep it so user only sees messages after deletion
                # The deletion timestamp should persist even after restoration

        # Email notifications are now handled automatically by Django signals
            
        # Send real-time conversation update (not new, just update)
        send_conversation_update(conversation, is_new=False, request=request)
        
        serializer = MessageSerializer(message, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class CreateConversationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_profile = request.user.userprofile
        recipient_phone = request.data.get("recipient_phone")
        if not recipient_phone:
            return Response({"error": "Recipient phone is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            recipient_profile = UserProfile.objects.get(phone_number=recipient_phone)
        except UserProfile.DoesNotExist:
            return Response({"error": "Recipient not found."}, status=status.HTTP_404_NOT_FOUND)

        # Check if conversation already exists
        conversation = (
            Conversation.objects
            .filter(participants=user_profile)
            .filter(participants=recipient_profile)
            .first()
        )
        if not conversation:
            conversation = Conversation.objects.create()
            conversation.participants.add(user_profile, recipient_profile)
            conversation.save()

        serializer = ConversationSerializer(conversation, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class EditMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, message_id):
        try:
            message = Message.objects.get(id=message_id, sender=request.user)
        except Message.DoesNotExist:
            return Response({"error": "Message not found or you don't have permission to edit it."}, 
                            status=status.HTTP_404_NOT_FOUND)
        
        # Only text messages can be edited
        if message.message_type != 'text':
            return Response({"error": "Only text messages can be edited."}, 
                            status=status.HTTP_400_BAD_REQUEST)
        
        content = request.data.get('content')
        if not content or not content.strip():
            return Response({"error": "Message content is required."}, 
                            status=status.HTTP_400_BAD_REQUEST)
        
        message.content = content.strip()
        message.save()
        
        serializer = MessageSerializer(message, context={'request': request})
        return Response(serializer.data)

class DeleteMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, message_id):
        try:
            message = Message.objects.get(id=message_id, sender=request.user)
        except Message.DoesNotExist:
            return Response({"error": "Message not found or you don't have permission to delete it."}, 
                            status=status.HTTP_404_NOT_FOUND)
        
        conversation_id = message.conversation.id
        message.delete()
        
        return Response({"success": True, "message": "Message deleted successfully", "conversation_id": conversation_id})

@method_decorator(csrf_exempt, name='dispatch')
class DeleteConversationView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, conversation_id):
        try:
            # Debug: Print user info
            print(f"User: {request.user.username}, User ID: {request.user.id}")
            print(f"User Profile: {request.user.userprofile.id if hasattr(request.user, 'userprofile') else 'No profile'}")
            print(f"Conversation ID: {conversation_id}")
            
            conversation = Conversation.objects.get(id=conversation_id, participants=request.user.userprofile)
            conversation.deleted_by.add(request.user.userprofile)
            
            # Store deletion timestamp

            deletion_timestamps = conversation.deletion_timestamps or {}
            deletion_timestamps[str(request.user.userprofile.id)] = timezone.now().isoformat()
            conversation.deletion_timestamps = deletion_timestamps
            conversation.save()
            
            return Response({"success": True, "message": "Conversation deleted successfully"})
        except Conversation.DoesNotExist:
            print(f"Conversation {conversation_id} not found or user {request.user.username} not a participant")
            return Response({"error": "Conversation not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            print(f"Error in delete_conversation: {str(e)}")
            return Response({"error": f"Server error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
