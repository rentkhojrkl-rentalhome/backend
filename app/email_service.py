import os
import resend
from dotenv import load_dotenv
import secrets

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

if not RESEND_API_KEY:
    raise RuntimeError("RESEND_API_KEY is missing from .env")

resend.api_key = RESEND_API_KEY


def send_test_email(to_email: str):
    return resend.Emails.send({
        "from": "RentKhojRKL <verify@rentkhojrkl.in>",
        "to": [to_email],
        "subject": "RentKhojRKL Email Verification Test",
        "html": """
            <div style="font-family: Arial, sans-serif;">
                <h2>Welcome to RentKhojRKL!</h2>
                <p>Your email sending configuration is being tested.</p>
                <p>Next, we will enable secure email OTP verification.</p>
            </div>
        """,
    })
def send_verification_otp(to_email: str, otp: str):
    return resend.Emails.send({
        "from": "RentKhojRKL <verify@rentkhojrkl.in>",
        "to": [to_email],
        "subject": "Your RentKhojRKL verification code",
        "html": f"""
        <div style="font-family:Arial,sans-serif;max-width:500px;margin:auto;">
            <h2 style="color:#218653;">Verify your email</h2>
            <p>Use this code to verify your RentKhojRKL account:</p>
            <div style="font-size:32px;font-weight:bold;letter-spacing:8px;
                        padding:20px;background:#f2f7f2;text-align:center;">
                {otp}
            </div>
            <p>This code expires in 10 minutes.</p>
            <p>If you did not request this code, you can ignore this email.</p>
            <p>— RentKhojRKL Team</p>
        </div>
        """,
    })
