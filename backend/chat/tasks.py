from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import timedelta
import logging

from .models import Message, EmailNotification
from users.models import UserProfile

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_email_notification(self, email_notification_id):
    """
    Send email notification for unread message
    """
    try:
        email_notification = EmailNotification.objects.get(id=email_notification_id)
        
        # Check if message has been read since scheduling
        if email_notification.message.is_read:
            logger.info(f"Message {email_notification.message.id} has been read, cancelling email")
            email_notification.cancel()
            return f"Email cancelled - message {email_notification.message.id} already read"
        
        # Check if recipient is now online
        recipient_profile = UserProfile.objects.get(user=email_notification.recipient)
        if recipient_profile.is_online:
            logger.info(f"Recipient {email_notification.recipient.username} is now online, cancelling email")
            email_notification.cancel()
            return f"Email cancelled - recipient {email_notification.recipient.username} is online"
        
        # Send the email
        send_mail(
            subject=email_notification.subject,
            message=email_notification.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email_notification.recipient_email],
            fail_silently=False,
        )
        
        # Mark as sent
        email_notification.mark_as_sent()
        
        logger.info(f"Email sent successfully to {email_notification.recipient_email} for message {email_notification.message.id}")
        
        # Schedule follow-up reminder if this was the first reminder
        if email_notification.is_first_reminder:
            schedule_follow_up_reminder.apply_async(
                args=[email_notification.message.id],
                countdown=3600  # 1 hour delay
            )
        
        return f"Email sent to {email_notification.recipient_email}"
        
    except EmailNotification.DoesNotExist:
        logger.error(f"EmailNotification {email_notification_id} not found")
        return f"EmailNotification {email_notification_id} not found"
    except Exception as exc:
        logger.error(f"Email sending failed: {str(exc)}")
        
        try:
            email_notification = EmailNotification.objects.get(id=email_notification_id)
            email_notification.retry_count += 1
            email_notification.mark_as_failed(str(exc))
        except:
            pass
        
        # Retry with exponential backoff
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
        
        return f"Email sending failed after {self.max_retries} retries: {str(exc)}"

@shared_task
def schedule_follow_up_reminder(message_id):
    """
    Schedule a follow-up email reminder 1 hour after the first one
    """
    try:
        message = Message.objects.get(id=message_id)
        
        # Check if message has been read
        if message.is_read:
            logger.info(f"Message {message_id} has been read, skipping follow-up reminder")
            return f"Follow-up cancelled - message {message_id} already read"
        
        # Check if recipient is online
        recipient_profile = UserProfile.objects.get(user=message.recipient.user)
        if recipient_profile.is_online:
            logger.info(f"Recipient {message.recipient.user.username} is online, skipping follow-up reminder")
            return f"Follow-up cancelled - recipient is online"
        
        # Create follow-up email notification
        sender_name = message.sender.get_full_name() or message.sender.username
        subject = f"Follow-up: New message from {sender_name}"
        
        message_preview = message.content[:100] + "..." if len(message.content) > 100 else message.content
        if message.message_type == 'audio':
            message_preview = "🎵 Audio message"
        
        body = f"""Hi {message.recipient.user.get_full_name() or message.recipient.user.username},

You still have an unread message from {sender_name}:

"{message_preview}"

This is a follow-up reminder since you haven't seen the message yet. 
Please log in to your chat to view and respond.

Best regards,
Your Chat App Team"""

        # Create follow-up email notification
        email_notification = EmailNotification.objects.create(
            message=message,
            recipient=message.recipient.user,
            recipient_email=message.recipient.user.email,
            scheduled_for=timezone.now(),
            subject=subject,
            body=body,
            is_first_reminder=False,
            is_follow_up=True
        )
        
        # Send immediately (follow-up reminder)
        result = send_email_notification.delay(email_notification.id)
        email_notification.celery_task_id = result.id
        email_notification.save(update_fields=['celery_task_id'])
        
        logger.info(f"Follow-up reminder scheduled for message {message_id}")
        return f"Follow-up reminder scheduled for message {message_id}"
        
    except Message.DoesNotExist:
        logger.error(f"Message {message_id} not found for follow-up reminder")
        return f"Message {message_id} not found"
    except Exception as exc:
        logger.error(f"Follow-up reminder scheduling failed: {str(exc)}")
        return f"Follow-up reminder scheduling failed: {str(exc)}"

@shared_task
def create_and_schedule_email_notification(message_id):
    """
    Create and schedule initial email notification for offline user
    """
    try:
        message = Message.objects.get(id=message_id)
        
        # Double-check recipient is offline and message is unread
        recipient_profile = UserProfile.objects.get(user=message.recipient.user)
        if recipient_profile.is_online or message.is_read:
            logger.info(f"Skipping email for message {message_id} - recipient online or message read")
            return f"Email skipped for message {message_id}"
        
        # Check if recipient has email
        if not message.recipient.user.email:
            logger.warning(f"No email address for user {message.recipient.user.username}")
            return f"No email address for recipient"
        
        # Create email content
        sender_name = message.sender.get_full_name() or message.sender.username
        subject = f"New message from {sender_name}"
        
        message_preview = message.content[:100] + "..." if len(message.content) > 100 else message.content
        if message.message_type == 'audio':
            message_preview = "🎵 Audio message"
        
        body = f"""Hi {message.recipient.user.get_full_name() or message.recipient.user.username},

You have received a new message from {sender_name}:

"{message_preview}"

Log in to your chat to view and respond to this message.

Best regards,
Your Chat App Team"""

        # Create email notification
        email_notification = EmailNotification.objects.create(
            message=message,
            recipient=message.recipient.user,
            recipient_email=message.recipient.user.email,
            scheduled_for=timezone.now(),
            subject=subject,
            body=body,
            is_first_reminder=True,
            is_follow_up=False
        )
        
        # Schedule email to be sent immediately
        result = send_email_notification.delay(email_notification.id)
        email_notification.celery_task_id = result.id
        email_notification.save(update_fields=['celery_task_id'])
        
        logger.info(f"Email notification created and scheduled for message {message_id}")
        return f"Email notification scheduled for message {message_id}"
        
    except Message.DoesNotExist:
        logger.error(f"Message {message_id} not found")
        return f"Message {message_id} not found"
    except Exception as exc:
        logger.error(f"Email notification creation failed: {str(exc)}")
        return f"Email notification creation failed: {str(exc)}"

@shared_task
def cancel_pending_notifications_for_message(message_id):
    """
    Cancel all pending email notifications for a message when it's read
    """
    try:
        pending_notifications = EmailNotification.objects.filter(
            message_id=message_id,
            status='pending'
        )
        
        cancelled_count = 0
        for notification in pending_notifications:
            notification.cancel()
            cancelled_count += 1
            
            # Try to revoke the Celery task if it hasn't started yet
            if notification.celery_task_id:
                from backend.celery import app
                app.control.revoke(notification.celery_task_id, terminate=True)
        
        logger.info(f"Cancelled {cancelled_count} pending notifications for message {message_id}")
        return f"Cancelled {cancelled_count} notifications"
        
    except Exception as exc:
        logger.error(f"Error cancelling notifications for message {message_id}: {str(exc)}")
        return f"Error cancelling notifications: {str(exc)}"

@shared_task
def cleanup_old_email_notifications():
    """
    Periodic task to clean up old email notifications (older than 30 days)
    """
    try:
        cutoff_date = timezone.now() - timedelta(days=30)
        deleted_count = EmailNotification.objects.filter(
            created_at__lt=cutoff_date
        ).delete()[0]
        
        logger.info(f"Cleaned up {deleted_count} old email notifications")
        return f"Cleaned up {deleted_count} old notifications"
        
    except Exception as exc:
        logger.error(f"Cleanup task failed: {str(exc)}")
        return f"Cleanup failed: {str(exc)}"
