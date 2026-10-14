
import asyncio
import getpass

from app.database import users_collection, client
from app.security import hash_password


async def main():
    email = input("Enter your new admin email: ").strip().lower()
    name = input("Enter your admin name: ").strip()

    if not email or "@" not in email or not name:
        print("Please enter a valid email and name.")
        return

    password = getpass.getpass("Create admin password (minimum 8 characters): ")

    if len(password) < 8 or len(password) > 128:
        print("Password must be between 8 and 128 characters.")
        return

    try:
        existing = await users_collection.find_one({"email": email})

        if existing:
            print("This email already exists. No account was changed.")
            return

        admin_user = {
            "name": name,
            "email": email,
            "password_hash": hash_password(password),
            "role": "admin",
        }

        await users_collection.insert_one(admin_user)

        print("\nAdmin account created successfully!")
        print(f"Email: {email}")
        print("Role: admin")
        print("You can now log in using your new email and password.")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
