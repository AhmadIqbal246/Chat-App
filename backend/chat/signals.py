from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Message


@receiver(post_save, sender=Message)
def trigger_email_notification(sender, instance, created, **kwargs):
    """Automatically trigger email notification when a new message is created"""
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[SIGNAL] Message post_save signal triggered: Message {instance.id}, created={created}")
    
    if created:  # Only for new messages
        logger.info(f"[SIGNAL] Processing new message {instance.id}")
        # Check if recipient is offline and has email
        recipient_profile = instance.recipient
        is_recipient_online = recipient_profile.is_online
        recipient_email = recipient_profile.user.email
        
        if not is_recipient_online and recipient_email:
            # Import here to avoid circular imports
            try:
                from .tasks import create_and_schedule_email_notification
                
                # Schedule email notification
                result = create_and_schedule_email_notification.delay(instance.id)
                
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"[SIGNAL] Email notification scheduled for message {instance.id} to {recipient_email} - Task ID: {result.id}")
                
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"[SIGNAL] Failed to schedule email notification for message {instance.id}: {str(e)}")
        else:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[SIGNAL] Skipping email for message {instance.id} - Recipient online: {is_recipient_online}, Has email: {bool(recipient_email)}")


@receiver(post_save, sender=Message)
def cancel_email_notification_on_read(sender, instance, created, **kwargs):
    """Cancel email notifications when a message is marked as read"""
    if not created and instance.is_read:  # Only for existing messages that are now read
        try:
            from .tasks import cancel_pending_notifications_for_message
            
            # Cancel pending email notifications
            result = cancel_pending_notifications_for_message.delay(instance.id)
            
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[SIGNAL] Email notifications cancelled for read message {instance.id} - Task ID: {result.id}")
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"[SIGNAL] Failed to cancel email notifications for message {instance.id}: {str(e)}")
