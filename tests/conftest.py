import os

os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATA_ENCRYPTION_KEY", "cXr3qGq3eGf1mG0nXgq9ZzJ3Yw9j6F8lQ2bN4sT1uVw=")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_finance.db")
