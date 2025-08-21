#!/usr/bin/env python3
"""
Server startup script for testing WebSocket connections
"""
import os
import django
import sys
from django.core.management import execute_from_command_line

if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
    
    # Try different ports if default is blocked
    ports_to_try = [8001, 8002, 8003, 8004, 8005]
    
    for port in ports_to_try:
        try:
            print(f"🚀 Attempting to start server on port {port}...")
            sys.argv = ['manage.py', 'runserver', f'127.0.0.1:{port}']
            execute_from_command_line(sys.argv)
            break
        except Exception as e:
            print(f"❌ Failed to start on port {port}: {e}")
            continue
    else:
        print("❌ Could not start server on any available port")
        sys.exit(1)
