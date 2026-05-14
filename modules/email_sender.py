"""Transactional email sender over Gmail SMTP.

Uses the same Gmail account + app password already configured for IMAP
inbox parsing. Gmail SMTP is free, instant, and allows ~500 messages/day —
enough for password resets without standing up a third-party email service.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_PORT, EMAIL_SMTP_SERVER


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send a single HTML email. Returns True on success, False on failure."""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("[email_sender] EMAIL_ADDRESS / EMAIL_PASSWORD not configured")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"JobFlow <{EMAIL_ADDRESS}>"
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    # Gmail app passwords are shown with spaces for readability but the SMTP
    # login expects them stripped.
    password = EMAIL_PASSWORD.replace(" ", "")

    try:
        with smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT, timeout=15) as server:
            server.login(EMAIL_ADDRESS, password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[email_sender] Failed to send to {to}: {e}")
        return False


def password_reset_html(action_link: str) -> str:
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f3ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:480px;margin:40px auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e9e6f5;">
    <div style="padding:32px 32px 8px;">
      <div style="font-size:22px;font-weight:800;color:#1a1a2e;">
        <span style="color:#6c5ce7;">Job</span>Flow
      </div>
    </div>
    <div style="padding:8px 32px 32px;">
      <h1 style="font-size:20px;color:#1a1a2e;margin:16px 0 8px;">Reset your password</h1>
      <p style="font-size:14px;color:#6b6b8a;line-height:1.6;margin:0 0 24px;">
        We got a request to reset your JobFlow password. Click the button below to choose a new one.
        This link expires in 1 hour. If you didn't request this, you can safely ignore this email.
      </p>
      <a href="{action_link}"
         style="display:inline-block;background:linear-gradient(135deg,#6c5ce7,#a78bfa);color:#ffffff;
                text-decoration:none;font-size:14px;font-weight:700;padding:12px 28px;border-radius:8px;">
        Reset password &rarr;
      </a>
      <p style="font-size:12px;color:#9b9bb0;line-height:1.6;margin:24px 0 0;">
        If the button doesn't work, paste this link into your browser:<br>
        <span style="color:#6c5ce7;word-break:break-all;">{action_link}</span>
      </p>
    </div>
  </div>
</body>
</html>
"""
