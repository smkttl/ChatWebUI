#!/usr/bin/env python3
"""Create a superuser manually. Run from the app directory."""
import os
import sys
import json
import hashlib
import getpass

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def create_superuser(username, email=""):
    userdata_dir = os.path.join(os.path.dirname(__file__), 'userdata')
    os.makedirs(userdata_dir, exist_ok=True)
    
    user_file = os.path.join(userdata_dir, f"{username}.json")
    if os.path.exists(user_file):
        print(f"Error: User '{username}' already exists")
        sys.exit(1)
    
    password = getpass.getpass("Enter password: ")
    if not password:
        print("Error: Password cannot be empty")
        sys.exit(1)
    
    user_data = {
        "username": username,
        "password_hash": hash_password(password),
        "email": email,
        "is_verified": True,
        "is_superuser": True,
        "created_at": None,
        "usage": {}
    }
    
    with open(user_file, 'w') as f:
        json.dump(user_data, f, indent=2)
    
    print(f"Superuser '{username}' created successfully")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python create_superuser.py <username> [email]")
        sys.exit(1)
    
    username = sys.argv[1]
    email = sys.argv[2] if len(sys.argv) > 2 else ""
    create_superuser(username, email)
