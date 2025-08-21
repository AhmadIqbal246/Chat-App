from django.db import models
from django.contrib.auth.models import User
from .models import Message

class EmailNotification(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='email_notifications')
    recipient = models.ForeignKey(User, on_delete=models.CASCADE)
    recipient_email = models.EmailField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Timing fields
    created_at = models.DateTimeField(auto_now_add=True)
    scheduled_for = models.DateTimeField()  # When to send the email
    sent_at = models.DateTimeField(null=True, blank=True)
    
    # Email content
    subject = models.CharField(max_length=255)
    body = models.TextField()
    
    # Tracking fields
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    
    # Retry tracking
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    
    # Email reminder specific fields
    is_first_reminder = models.BooleanField(default=True)
    is_follow_up = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'status']),
            models.Index(fields=['scheduled_for']),
            models.Index(fields=['message']),
        ]
    
    def __str__(self):
        return f"Email to {self.recipient_email} for message {self.message.id} - {self.status}"
    
    def mark_as_sent(self):
        """Mark email as successfully sent"""
        from django.utils import timezone
        self.status = 'sent'
        self.sent_at = timezone.now()
        self.save(update_fields=['status', 'sent_at'])
    
    def mark_as_failed(self, error_message=None):
        """Mark email as failed"""
        self.status = 'failed'
        if error_message:
            self.error_message = error_message
        self.save(update_fields=['status', 'error_message'])
    
    def cancel(self):
        """Cancel pending email"""
        if self.status == 'pending':
            self.status = 'cancelled'
            self.save(update_fields=['status'])
