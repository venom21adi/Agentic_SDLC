#!/usr/bin/env python3
"""
Setup script to initialize the Agentic SDLC system.
Starts PostgreSQL, creates tables, and tests the connection.
"""

import subprocess
import time
import sys
import os
from pathlib import Path

def run_command(cmd, description):
    """Run a shell command and report status."""
    print(f"\n{description}...")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Failed: {result.stderr}")
        return False
    print(f"✅ {description} completed")
    return True

def main():
    print("=" * 60)
    print("Agentic SDLC - Setup Script")
    print("=" * 60)

    project_root = Path(__file__).parent

    # Step 1: Create .env if it doesn't exist
    env_file = project_root / ".env"
    if not env_file.exists():
        print("\nCreating .env file...")
        env_example = project_root / ".env.example"
        if env_example.exists():
            with open(env_example) as f:
                content = f.read()
            with open(env_file, 'w') as f:
                f.write(content)
            print("✅ .env file created. Please edit it and add your DEEPSEEK_API_KEY (or OPENAI_API_KEY)")
        else:
            print("⚠️  .env.example not found, please create .env manually")
    else:
        print("✅ .env file already exists")

    # Step 2: Start Docker containers
    print("\nStarting PostgreSQL...")
    if run_command("docker-compose up -d", "Starting PostgreSQL"):
        time.sleep(3)  # Wait for DB to be ready

    # Step 3: Test database connection
    print("\nTesting database connection...")
    try:
        from state_store import StateStore
        store = StateStore()
        store.init_db()
        print("✅ Database connection successful and tables created")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        print("Make sure PostgreSQL is running: docker-compose up -d")
        return False

    print("\n" + "=" * 60)
    print("Setup complete! 🎉")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Edit .env and add your DEEPSEEK_API_KEY (or OPENAI_API_KEY)")
    print("2. Run: python main.py")
    print("\nDatabase status:")
    print("  - Container: agentic-sdlc-postgres")
    print("  - Database: agentic_sdlc")
    print("  - User: sdlc")
    print("  - Port: 5432")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
